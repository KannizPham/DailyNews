"""Stage 4 — Deliver (§8 của brief): format Telegram + gửi qua Bot API.

Tôn trọng DRY_RUN: true -> in console, không gọi Telegram API thật.
Nhãn 🟢🟡🔴 giữ nguyên trong text, không bị format/escape mất.

Quyết định: dùng parse_mode="HTML" (không phải "Markdown" legacy của Telegram).
Lý do: LLM trả markdown kiểu GFM (### header, **bold**) không tương thích cú
pháp Markdown legacy của Telegram (chỉ hiểu *bold* một dấu sao, không hiểu #
header) -> hiển thị sai/lộ ký tự thô. HTML mode bền hơn: convert rõ ràng từng
pattern markdown sang tag, ký tự thô còn lại (như "*" trong bullet list) không
có ý nghĩa đặc biệt trong HTML nên không bị Telegram từ chối parse.

Phần tương tác real-time (Cloudflare Worker, xem worker/): hàm
`write_items_to_kv()` ghi context từng item đã verify lên Cloudflare KV để
Worker đọc lại khi operator bấm nút "🔍 Hỏi sâu thêm" hoặc hỏi follow-up tự
do. `build_inline_keyboard()` tạo `reply_markup` gắn nút này dưới mỗi digest.
Toàn bộ phần này là OPTIONAL: nếu thiếu secret Cloudflare, log warning và bỏ
qua, KHÔNG sập pipeline (đúng nguyên tắc cũ của project).
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from llm_client import sanitize_sensitive_text
from memory import is_valid_digest_text

logger = logging.getLogger(__name__)
VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_PHOTO_API_URL = "https://api.telegram.org/bot{token}/sendPhoto"
TELEGRAM_MAX_LEN = 4096
DEFAULT_TIMEOUT_S = 20

CLOUDFLARE_KV_URL = (
    "https://api.cloudflare.com/client/v4/accounts/{account_id}"
    "/storage/kv/namespaces/{namespace_id}/values/{key}"
)
# TTL item context trong KV — đủ cho operator bấm "Hỏi sâu thêm" vài ngày
# sau, không cần giữ vĩnh viễn (free tier KV có giới hạn dung lượng).
KV_TTL_SECONDS = 48 * 3600
# raw_text rút gọn trước khi ghi KV, tránh payload quá lớn / lộ quá nhiều
# context không cần thiết cho 1 câu hỏi đào sâu.
KV_RAW_TEXT_MAX_LEN = 1500

# Thứ tự thứ trong tuần tiếng Việt cho header.
WEEKDAY_VI = [
    "Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật",
]


def format_header(now: Optional[datetime] = None) -> str:
    if now is None:
        now = datetime.now(VIETNAM_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=VIETNAM_TZ)
    else:
        now = now.astimezone(VIETNAM_TZ)
    weekday = WEEKDAY_VI[now.weekday()]
    return (
        "📰 Bản Tin Kinh Tế & Thị Trường — "
        f"{now.strftime('%d/%m/%Y')} ({weekday})"
    )


def markdown_to_telegram_html(text: str) -> str:
    """Convert markdown kiểu GFM (LLM hay trả ra, không cố định ### hay không)
    sang HTML mà Telegram parse_mode="HTML" hiểu được.

    Thứ tự xử lý quan trọng: escape HTML đặc biệt TRƯỚC khi chèn tag, để
    không tự phá tag mình vừa chèn.
    """
    # 1. Escape ký tự đặc biệt HTML trước (nếu không, "<" trong text thường sẽ
    # phá cấu trúc tag chèn ở bước sau).
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 2. Heading markdown (#, ##, ### ...) ở đầu dòng -> <b>...</b>
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # 3. **bold** (GFM) -> <b>...</b>. Phải làm TRƯỚC bước *italic* single-star
    # vì **x** chứa *x* lồng bên trong.
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    # 4. Markdown link [text](url) -> <a href="url">text</a>
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text)

    # 5. Bullet "* " hoặc "*   " ở đầu dòng -> "- " (operator thấy "*" đầu
    # dòng xấu trên Telegram). Làm SAU bước bold (**) ở trên nên "*" còn lại
    # ở đầu dòng chắc chắn là bullet marker, không phải markup bold.
    text = re.sub(r"^\*\s+", "- ", text, flags=re.MULTILINE)

    return text


def build_message(digest_text: str, now: Optional[datetime] = None) -> str:
    header = format_header(now)
    body = markdown_to_telegram_html(digest_text.strip())
    return f"{header}\n\n{body}"


def split_message(text: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Tách message dài thành nhiều phần, cố gắng cắt theo ranh giới dòng để
    không cắt giữa 1 câu/nhãn confidence."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        cut_at = remaining.rfind("\n", 0, max_len)
        if cut_at <= 0:
            cut_at = max_len
        chunks.append(remaining[:cut_at].rstrip())
        remaining = remaining[cut_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def send_telegram_message(
    token: str,
    chat_id: str,
    text: str,
    reply_markup: Optional[dict] = None,
) -> bool:
    url = TELEGRAM_API_URL.format(token=token)
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=DEFAULT_TIMEOUT_S)
        if resp.status_code != 200:
            logger.warning(
                "deliver.py: Telegram chat=%s trả status %s: %s",
                chat_id,
                resp.status_code,
                sanitize_sensitive_text(resp.text, token),
            )
            return False
        return True
    except requests.RequestException as exc:
        logger.warning("deliver.py: lỗi gọi Telegram API chat=%s: %s", chat_id, sanitize_sensitive_text(exc, token))
        return False


def send_telegram_photo(token: str, chat_id: str, photo_url: str, caption: str) -> bool:
    """Gửi ảnh thumbnail (og:image lấy từ enrich.py) kèm caption ngắn, đứng
    TRƯỚC digest text — chỉ best-effort: ảnh hỏng/link chết KHÔNG được làm
    fail toàn bộ deliver (xem deliver(), gọi hàm này tách riêng send text)."""
    url = TELEGRAM_PHOTO_API_URL.format(token=token)
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "photo": photo_url, "caption": caption},
            timeout=DEFAULT_TIMEOUT_S,
        )
        if resp.status_code != 200:
            logger.warning(
                "deliver.py: Telegram sendPhoto trả status %s: %s — bỏ qua ảnh, "
                "vẫn gửi digest text bình thường.",
                resp.status_code, sanitize_sensitive_text(resp.text, token),
            )
            return False
        return True
    except requests.RequestException as exc:
        logger.warning("deliver.py: lỗi gọi Telegram sendPhoto: %s — bỏ qua ảnh.", sanitize_sensitive_text(exc, token))
        return False


def build_inline_keyboard(verified_items: list) -> Optional[dict]:
    """Tạo reply_markup inline keyboard: mỗi item đã verify -> 1 nút
    "🔍 Hỏi sâu thêm: <tên ngắn>" với callback_data="qa:<key>".

    `<key>` = index 1-N trong list (ổn định trong phạm vi 1 lần gửi, đơn giản
    hơn hash item.id và đủ để Worker khớp lại với key đã ghi trong KV qua
    `write_items_to_kv` — cả hai dùng cùng index nên luôn nhất quán).

    Trả None nếu verified_items rỗng (không có gì để gắn nút).
    """
    if not verified_items:
        return None

    buttons = []
    for idx, v in enumerate(verified_items, start=1):
        title = getattr(v.item, "title", "") or "item"
        short_title = title[:30] + ("…" if len(title) > 30 else "")
        buttons.append(
            [{"text": f"🔍 Hỏi sâu thêm: {short_title}", "callback_data": f"qa:{idx}"}]
        )
    return {"inline_keyboard": buttons}


def _kv_put(
    key: str,
    value: str,
    account_id: str,
    namespace_id: str,
    api_token: str,
    ttl_seconds: int = KV_TTL_SECONDS,
) -> bool:
    url = CLOUDFLARE_KV_URL.format(account_id=account_id, namespace_id=namespace_id, key=key)
    try:
        resp = requests.put(
            url,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "text/plain; charset=utf-8",
            },
            params={"expiration_ttl": ttl_seconds},
            data=value.encode("utf-8"),
            timeout=DEFAULT_TIMEOUT_S,
        )
        try:
            result = resp.json()
        except ValueError:
            result = None
        if resp.status_code != 200 or not isinstance(result, dict) or result.get("success") is not True:
            logger.warning(
                "deliver.py: Cloudflare KV PUT key=%s trả status %s: %s",
                key,
                resp.status_code,
                sanitize_sensitive_text(resp.text, api_token),
            )
            return False
        return True
    except requests.RequestException as exc:
        logger.warning(
            "deliver.py: lỗi gọi Cloudflare KV API (key=%s): %s",
            key,
            sanitize_sensitive_text(exc, api_token),
        )
        return False


def write_items_to_kv(verified_items: list) -> bool:
    """Ghi context từng item đã verify lên Cloudflare KV để Worker đọc lại
    khi operator bấm nút "Hỏi sâu thêm" hoặc hỏi follow-up tự do.

    Ghi 2 dạng key:
      - "item:<idx>" (idx khớp với callback_data của build_inline_keyboard)
        chứa context 1 item (title/url/raw_text rút gọn/type/confidence).
      - "latest_items" chứa toàn bộ list (cho nhánh free-text Q&A ở Worker).

    OPTIONAL: nếu thiếu bất kỳ secret Cloudflare nào (CLOUDFLARE_API_TOKEN,
    CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_KV_NAMESPACE_ID), log warning và trả
    False, KHÔNG raise — pipeline chính vẫn phải chạy tiếp bình thường.
    """
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    namespace_id = os.environ.get("CLOUDFLARE_KV_NAMESPACE_ID")

    if not api_token or not account_id or not namespace_id:
        logger.warning(
            "deliver.py: thiếu CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID/"
            "CLOUDFLARE_KV_NAMESPACE_ID trong env -> bỏ qua ghi KV (tính năng "
            "'Hỏi sâu thêm'/'/refresh' qua Worker sẽ không hoạt động cho lần "
            "gửi này, nhưng digest vẫn gửi bình thường)."
        )
        return False

    if not verified_items:
        return False

    all_items_payload = []
    all_ok = True
    for idx, v in enumerate(verified_items, start=1):
        item = v.item
        item_payload = {
            "title": getattr(item, "title", ""),
            "url": getattr(item, "url", ""),
            "raw_text": (getattr(item, "raw_text", "") or "")[:KV_RAW_TEXT_MAX_LEN],
            "type": getattr(item, "type", ""),
            "confidence": getattr(v, "confidence_emoji", "") or getattr(v, "confidence", ""),
        }
        all_items_payload.append(item_payload)

        ok = _kv_put(
            key=f"item:{idx}",
            value=json.dumps(item_payload, ensure_ascii=False),
            account_id=account_id,
            namespace_id=namespace_id,
            api_token=api_token,
        )
        all_ok = all_ok and ok

    ok_latest = _kv_put(
        key="latest_items",
        value=json.dumps(all_items_payload, ensure_ascii=False),
        account_id=account_id,
        namespace_id=namespace_id,
        api_token=api_token,
    )
    all_ok = all_ok and ok_latest

    if all_ok:
        logger.info("deliver.py: đã ghi %d item + 'latest_items' lên Cloudflare KV.", len(verified_items))
    else:
        logger.warning("deliver.py: ghi Cloudflare KV có lỗi một phần, xem warning ở trên.")

    return all_ok


# Dữ liệu xu hướng được tính theo document frequency trên từng digest archive,
# không dùng count tích luỹ trong kb.json. Mỗi topic chỉ được tính tối đa một
# lần cho một ngày, dù từ khoá lặp lại bao nhiêu lần trong digest đó.
TREND_SCHEMA_VERSION = 2
TREND_TOP_N = 5
TREND_WINDOW_DAYS = 7
TREND_FALLBACK_WINDOW_DAYS = 14
TREND_MIN_DIGEST_DAYS = 3
KB_TRENDS_TTL_SECONDS = 30 * 24 * 3600
DEFAULT_ARCHIVE_DIR = Path(__file__).resolve().parents[1] / "archive"

# Regex chạy trên text đã lower-case và bỏ dấu tiếng Việt. Các pattern ngắn
# như "AI" không được dùng độc lập để OpenAI/AI Agents không tự biến thành
# một xu hướng công nghệ; AI chỉ được tính khi có ngữ cảnh đầu tư/chi tiêu.
TREND_TAXONOMY = (
    ("inflation", "Lạm phát", "Macro", (r"\binflation\b", r"\bcpi\b", r"lam phat", r"consumer prices?")),
    ("interest_rates", "Lãi suất", "Macro", (r"interest rates?", r"policy rates?", r"lai suat dieu hanh", r"lai suat chinh sach", r"cat giam lai suat", r"tang lai suat", r"fed funds?")),
    ("exchange_rates", "Tỷ giá", "Macro", (r"usd\s*[/\-]?\s*vnd", r"exchange rates?", r"ty gia", r"currency depreciation", r"dong tien mat gia", r"vnd mat gia")),
    ("gdp_growth", "Tăng trưởng GDP", "Macro", (r"\bgdp\b", r"gross domestic product", r"tang truong kinh te", r"tang truong tong san pham")),
    ("fiscal_policy", "Chính sách tài khóa", "Macro", (r"fiscal polic", r"chinh sach tai khoa", r"government spending", r"chi tieu cong", r"ngan sach nha nuoc", r"budget deficit", r"tham hut ngan sach")),
    ("monetary_policy", "Chính sách tiền tệ", "Macro", (r"monetary polic", r"chinh sach tien te", r"money supply", r"cung tien")),
    ("employment", "Việc làm", "Macro", (r"employment", r"unemployment", r"labor market", r"labour market", r"viec lam", r"that nghiep", r"thi truong lao dong")),
    ("commodity_prices", "Giá hàng hóa", "Macro", (r"commodity prices?", r"gia hang hoa", r"hang hoa co ban", r"commodity index")),
    ("credit_growth", "Tăng trưởng tín dụng", "Banking", (r"credit growth", r"tang truong tin dung", r"tin dung tang", r"credit expansion")),
    ("deposit_rates", "Lãi suất tiền gửi", "Banking", (r"deposit rates?", r"lai suat tien gui", r"lai suat huy dong")),
    ("bad_debt", "Nợ xấu", "Banking", (r"\bnpl(?:s)?\b", r"non[- ]performing loans?", r"bad debt", r"no xau")),
    ("liquidity", "Thanh khoản ngân hàng", "Banking", (r"bank(?:ing)? liquidity", r"thanh khoan ngan hang", r"interbank liquidity", r"thanh khoan lien ngan hang")),
    ("capital_adequacy", "An toàn vốn", "Banking", (r"capital adequacy", r"capital ratio", r"he so an toan von", r"ty le an toan von", r"\bcar\b")),
    ("central_bank_policy", "Chính sách ngân hàng trung ương", "Banking", (r"central bank polic", r"chinh sach ngan hang trung uong", r"open market operations?", r"nghiep vu thi truong mo", r"reserve requirements?", r"du tru bat buoc")),
    ("vietnam_stock_market", "Thị trường chứng khoán Việt Nam", "Markets", (r"vn[- ]?index", r"vietnam(?:ese)? equities", r"vietnam(?:ese)? stocks?", r"chung khoan viet nam", r"co phieu viet nam")),
    ("global_equities", "Chứng khoán quốc tế", "Markets", (r"global equities", r"global stocks?", r"chung khoan quoc te", r"s&p\s*500", r"nasdaq", r"dow jones", r"nikkei")),
    ("bonds", "Trái phiếu", "Markets", (r"\bbonds?\b", r"trai phieu", r"government yields?", r"treasury yields?")),
    ("gold", "Vàng", "Markets", (r"\bgold\b", r"gia vang", r"vang sjc")),
    ("oil", "Dầu mỏ", "Markets", (r"crude oil", r"brent", r"\bwti\b", r"gia dau", r"dau mo")),
    ("foreign_flows", "Dòng vốn ngoại", "Markets", (r"foreign flows?", r"foreign investors?", r"foreign net (?:buying|selling)", r"dong von ngoai", r"khoi ngoai", r"nha dau tu nuoc ngoai")),
    ("market_valuation", "Định giá thị trường", "Markets", (r"market valuation", r"dinh gia thi truong", r"market p/?e", r"forward p/?e")),
    ("earnings", "Kết quả kinh doanh", "Corporate", (r"\bearnings\b", r"business results?", r"ket qua kinh doanh", r"doanh thu", r"loi nhuan")),
    ("ma", "Mua bán và sáp nhập", "Corporate", (r"\bm\s*&\s*a\b", r"mergers? and acquisitions?", r"mua ban va sap nhap", r"thau tom")),
    ("capital_raising", "Huy động vốn", "Corporate", (r"capital raising", r"raise[ds]? capital", r"huy dong von", r"phat hanh them co phieu")),
    ("corporate_debt", "Nợ doanh nghiệp", "Corporate", (r"corporate debt", r"no doanh nghiep", r"corporate bonds?", r"trai phieu doanh nghiep")),
    ("valuation", "Định giá doanh nghiệp", "Corporate", (r"company valuation", r"enterprise valuation", r"dinh gia doanh nghiep", r"dinh gia cong ty")),
    ("business_restructuring", "Tái cấu trúc doanh nghiệp", "Corporate", (r"business restructuring", r"corporate restructuring", r"tai cau truc doanh nghiep", r"tai co cau doanh nghiep")),
    ("trade_tensions", "Căng thẳng thương mại", "Geopolitics", (r"trade tensions?", r"trade war", r"cang thang thuong mai", r"chien tranh thuong mai")),
    ("tariffs", "Thuế quan", "Geopolitics", (r"\btariffs?\b", r"thue quan")),
    ("sanctions", "Trừng phạt", "Geopolitics", (r"\bsanctions?\b", r"trung phat kinh te", r"lenh trung phat")),
    ("supply_chains", "Chuỗi cung ứng", "Geopolitics", (r"supply chains?", r"chuoi cung ung")),
    ("regional_conflict", "Xung đột khu vực", "Geopolitics", (r"regional conflicts?", r"xung dot khu vuc", r"military conflict", r"xung dot quan su")),
    ("ai_investment", "Đầu tư AI", "Technology", (r"ai investment", r"investment in ai", r"ai spending", r"ai capex", r"dau tu (?:vao )?ai", r"chi tieu (?:cho )?ai")),
    ("semiconductors", "Bán dẫn", "Technology", (r"semiconductors?", r"chipmaking", r"chip industry", r"ban dan", r"nganh chip")),
    ("fintech", "Công nghệ tài chính", "Technology", (r"\bfintech\b", r"financial technology", r"cong nghe tai chinh")),
    ("digital_banking", "Ngân hàng số", "Technology", (r"digital banking", r"digital banks?", r"ngan hang so")),
    ("technology_regulation", "Quản lý công nghệ", "Technology", (r"technology regulation", r"tech regulation", r"ai regulation", r"quan ly cong nghe", r"quy dinh ve ai", r"luat ai")),
)


def _normalize_trend_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _topics_in_digest(text: str) -> set[str]:
    normalized = _normalize_trend_text(text)
    matched = {
        topic_id
        for topic_id, _label, _category, patterns in TREND_TAXONOMY
        if any(re.search(pattern, normalized) for pattern in patterns)
    }

    # Fed/SBV/ECB/ngân hàng trung ương được phân loại theo ngữ cảnh: nếu đi
    # cùng tín hiệu lãi suất thì là Lãi suất, nếu không thì là chính sách NHTW.
    central_bank = re.search(
        r"\b(?:fed|federal reserve|sbv|ecb|central bank|state bank of vietnam)\b|"
        r"ngan hang (?:nha nuoc|trung uong)",
        normalized,
    )
    rate_context = re.search(
        r"interest rates?|policy rates?|rate (?:cut|hike)s?|lai suat|fed funds?",
        normalized,
    )
    if central_bank:
        matched.add("interest_rates" if rate_context else "central_bank_policy")

    return matched


def load_digest_documents(archive_dir: Path = DEFAULT_ARCHIVE_DIR) -> list[tuple[date, str]]:
    """Đọc archive daily; mỗi file lỗi được bỏ qua độc lập."""
    if not archive_dir.exists():
        return []

    documents: list[tuple[date, str]] = []
    for path in archive_dir.glob("*.md"):
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.md", path.name)
        if not match:
            continue
        try:
            digest_date = date.fromisoformat(match.group(1))
            content = path.read_text(encoding="utf-8")
            if not is_valid_digest_text(content):
                logger.warning("deliver.py: bỏ archive không hợp lệ khỏi trends: %s", path)
                continue
            documents.append((digest_date, content))
        except (OSError, UnicodeError, ValueError) as exc:
            logger.warning("deliver.py: bỏ qua archive lỗi %s: %s", path, exc)
    return documents


def _trend_direction(recent_count: int, earlier_count: int) -> str:
    if recent_count >= earlier_count + 1:
        return "Tăng"
    if earlier_count >= recent_count + 1:
        return "Giảm"
    return "Ổn định"


def build_trends_summary(
    documents: list[tuple[date | str, str]],
    as_of: Optional[date] = None,
) -> dict:
    """Tính top trend theo số digest có nhắc topic trong rolling window."""
    as_of = as_of or datetime.now(VIETNAM_TZ).date()

    # Gom theo ngày để một topic không bao giờ được đếm quá một lần/digest day.
    by_day: dict[date, list[str]] = {}
    for raw_date, text in documents:
        try:
            digest_date = raw_date if isinstance(raw_date, date) else date.fromisoformat(raw_date)
        except (TypeError, ValueError):
            continue
        if digest_date > as_of:
            continue
        by_day.setdefault(digest_date, []).append(text or "")

    def select(window_days: int) -> dict[date, list[str]]:
        start = as_of - timedelta(days=window_days - 1)
        return {day: texts for day, texts in by_day.items() if start <= day <= as_of}

    window_days = TREND_WINDOW_DAYS
    selected = select(window_days)
    if len(selected) < TREND_MIN_DIGEST_DAYS:
        window_days = TREND_FALLBACK_WINDOW_DAYS
        selected = select(window_days)

    topics_by_day = {
        day: _topics_in_digest("\n".join(texts))
        for day, texts in selected.items()
    }
    recent_days = (window_days + 1) // 2
    recent_start = as_of - timedelta(days=recent_days - 1)
    taxonomy_by_id = {
        topic_id: (label, category)
        for topic_id, label, category, _patterns in TREND_TAXONOMY
    }

    trends: list[dict] = []
    for topic_id, (label, category) in taxonomy_by_id.items():
        mention_days = sorted(day for day, topics in topics_by_day.items() if topic_id in topics)
        if not mention_days:
            continue
        recent_count = sum(day >= recent_start for day in mention_days)
        earlier_count = len(mention_days) - recent_count
        trends.append(
            {
                "topic_id": topic_id,
                "label_vi": label,
                "category": category,
                "count": len(mention_days),
                "last_seen": mention_days[-1].isoformat(),
                "direction": _trend_direction(recent_count, earlier_count),
            }
        )

    trends.sort(
        key=lambda item: (
            item["count"],
            sum(
                day >= recent_start
                for day, topics in topics_by_day.items()
                if item["topic_id"] in topics
            ),
            item["last_seen"],
            item["label_vi"],
        ),
        reverse=True,
    )
    return {
        "schema_version": TREND_SCHEMA_VERSION,
        "window_days": window_days,
        "digest_days": len(selected),
        "minimum_digest_days": TREND_MIN_DIGEST_DAYS,
        "needed_digest_days": max(0, TREND_MIN_DIGEST_DAYS - len(selected)),
        "as_of": as_of.isoformat(),
        "trends": trends[:TREND_TOP_N],
    }


def write_trends_to_kv(_kb: Optional[dict] = None, archive_dir: Path = DEFAULT_ARCHIVE_DIR) -> bool:
    """Ghi rolling trend summary từ archive lên KV key ``trends:latest``.

    ``_kb`` được giữ để tương thích với call site hiện tại; count lịch sử trong
    kb.json không còn tham gia tính xu hướng.

    OPTIONAL: thiếu secret Cloudflare -> log warning, trả False, KHÔNG
    raise (đúng nguyên tắc graceful-degrade của project)."""
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    namespace_id = os.environ.get("CLOUDFLARE_KV_NAMESPACE_ID")

    if not api_token or not account_id or not namespace_id:
        logger.warning(
            "deliver.py: thiếu CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID/"
            "CLOUDFLARE_KV_NAMESPACE_ID -> bỏ qua ghi 'trends:latest' (lệnh "
            "'/trends' trên Worker sẽ không có dữ liệu cho tới khi set secret)."
        )
        return False

    documents = load_digest_documents(archive_dir)
    summary = build_trends_summary(documents)
    as_of = summary["as_of"]
    today_text = "\n".join(text for day, text in documents if day.isoformat() == as_of)
    snapshot = {
        "schema_version": TREND_SCHEMA_VERSION,
        "date": as_of,
        "topic_ids": sorted(_topics_in_digest(today_text)),
    }
    snapshot_ok = _kv_put(
        key=f"trend-day:{as_of}",
        value=json.dumps(snapshot, ensure_ascii=False),
        account_id=account_id,
        namespace_id=namespace_id,
        api_token=api_token,
        ttl_seconds=KB_TRENDS_TTL_SECONDS,
    )
    latest_ok = _kv_put(
        key="trends:latest",
        value=json.dumps(summary, ensure_ascii=False),
        account_id=account_id,
        namespace_id=namespace_id,
        api_token=api_token,
        ttl_seconds=KB_TRENDS_TTL_SECONDS,
    )
    if snapshot_ok and latest_ok:
        logger.info(
            "deliver.py: đã ghi 'trends:latest' (%d ngày, top %d) lên Cloudflare KV.",
            summary["window_days"],
            TREND_TOP_N,
        )
    return snapshot_ok and latest_ok


def get_telegram_chat_ids() -> list[str]:
    """Đọc danh sách chat, giữ chat ID âm của group và loại trùng ổn định."""
    raw = (os.environ.get("TELEGRAM_CHAT_IDS") or "").strip()
    if not raw:
        raw = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    result: list[str] = []
    for value in raw.split(","):
        chat_id = value.strip()
        if not chat_id:
            continue
        if not re.fullmatch(r"-?\d+", chat_id):
            logger.warning("deliver.py: bỏ TELEGRAM_CHAT_IDS không hợp lệ: %s", chat_id)
            continue
        if chat_id not in result:
            result.append(chat_id)
    return result


def notify_operator(text: str, *, dry_run: bool = False) -> bool:
    """Gửi thông báo vận hành plain-text tới mọi chat cấu hình."""
    if dry_run:
        print(f"[DRY_RUN operator notice] {text}")
        return True
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_ids = get_telegram_chat_ids()
    if not token or not chat_ids:
        logger.warning("deliver.py: thiếu TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_IDS cho operator notice.")
        return False
    all_ok = True
    for chat_id in chat_ids:
        all_ok = send_telegram_message(token, chat_id, text) and all_ok
    return all_ok


def deliver(
    digest_text: str,
    dry_run: bool = True,
    now: Optional[datetime] = None,
    reply_markup: Optional[dict] = None,
    photo_url: Optional[str] = None,
) -> bool:
    """Stage 4 đầy đủ: build message, tách nếu quá dài, gửi (hoặc in console
    nếu DRY_RUN). Trả True nếu mọi phần gửi thành công (hoặc dry_run).

    reply_markup (nếu có) chỉ gắn vào CHUNK CUỐI CÙNG — nút "Hỏi sâu thêm"
    nên đứng ngay dưới phần cuối digest, không lặp lại ở message tách giữa.

    photo_url (nếu có, lấy từ enrich.enrich_items): gửi TRƯỚC digest text như
    1 message ảnh riêng kèm caption ngày — best-effort, lỗi ảnh không chặn
    việc gửi digest text (operator cần đọc tin hơn cần ảnh đẹp).
    """
    full_message = build_message(digest_text, now=now)
    chunks = split_message(full_message)

    if dry_run:
        if photo_url:
            print(f"[DRY_RUN] sẽ gửi ảnh thumbnail trước: {photo_url}")
        print("=" * 70)
        print("DRY_RUN=true — KHÔNG gọi Telegram API thật. Nội dung sẽ gửi:")
        print("=" * 70)
        for i, chunk in enumerate(chunks, start=1):
            if len(chunks) > 1:
                print(f"\n--- Phần {i}/{len(chunks)} ---")
            print(chunk)
        if reply_markup:
            print("\n--- reply_markup (inline keyboard) sẽ gắn vào chunk cuối ---")
            print(json.dumps(reply_markup, ensure_ascii=False, indent=2))
        return True

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_ids = get_telegram_chat_ids()

    if not token or not chat_ids:
        logger.warning(
            "deliver.py: thiếu TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_IDS trong "
            "env -> không gửi được, in ra console thay thế (không crash)."
        )
        for chunk in chunks:
            print(chunk)
        return False

    all_ok = True
    for chat_id in chat_ids:
        if photo_url:
            send_telegram_photo(token, chat_id, photo_url, caption=format_header(now))
        last_idx = len(chunks) - 1
        chat_ok = True
        for i, chunk in enumerate(chunks):
            markup = reply_markup if i == last_idx else None
            chat_ok = send_telegram_message(token, chat_id, chunk, reply_markup=markup) and chat_ok
        all_ok = chat_ok and all_ok

    return all_ok

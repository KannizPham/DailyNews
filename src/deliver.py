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
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)

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
    now = now or datetime.now()
    weekday = WEEKDAY_VI[now.weekday()]
    return f"📰 Morning Intel — {now.strftime('%d/%m/%Y')} ({weekday})"


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
                "deliver.py: Telegram trả status %s: %s", resp.status_code, resp.text
            )
            return False
        return True
    except requests.RequestException as exc:
        logger.warning("deliver.py: lỗi gọi Telegram API: %s", exc)
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
                resp.status_code, resp.text,
            )
            return False
        return True
    except requests.RequestException as exc:
        logger.warning("deliver.py: lỗi gọi Telegram sendPhoto: %s — bỏ qua ảnh.", exc)
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
        # QUAN TRỌNG: API Cloudflare KV (PUT .../values/{key}) yêu cầu body
        # multipart/form-data cho field "value"/"expiration_ttl" (xem docs
        # Cloudflare "Write key-value pair"). Dùng requests.put(..., data=dict)
        # gửi application/x-www-form-urlencoded -> Cloudflare KHÔNG parse được
        # field, lưu nguyên văn chuỗi "value=...&expiration_ttl=..." làm value
        # (bug thật đã phát hiện: Worker đọc lại bị "JSON.parse" lỗi vì value
        # không phải JSON nữa). Dùng `files=` với tuple (None, x) để buộc
        # requests encode đúng multipart/form-data, không phải url-encoded.
        resp = requests.put(
            url,
            headers={"Authorization": f"Bearer {api_token}"},
            files={
                "value": (None, value),
                "expiration_ttl": (None, str(ttl_seconds)),
            },
            timeout=DEFAULT_TIMEOUT_S,
        )
        if resp.status_code != 200:
            logger.warning(
                "deliver.py: Cloudflare KV PUT key=%s trả status %s: %s",
                key, resp.status_code, resp.text,
            )
            return False
        return True
    except requests.RequestException as exc:
        logger.warning("deliver.py: lỗi gọi Cloudflare KV API (key=%s): %s", key, exc)
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


# Top-N mỗi mục (themes/companies/tech/deep_tech) ghi lên KV cho lệnh
# Telegram "/trends" — kb.json đầy đủ sẽ phình to theo thời gian, không nên
# đẩy nguyên file lên KV (free tier giới hạn dung lượng value, và Worker
# chỉ cần top items để trả lời, không cần toàn bộ lịch sử).
KB_TRENDS_TOP_N = 15
# "trends:latest" là lớp tích luỹ dài hạn (khác item context 48h) — giữ
# TTL dài (30 ngày) để vẫn còn dữ liệu nếu pipeline lỡ 1-2 ngày không chạy,
# nhưng không set vĩnh viễn (key sẽ được pipeline ngày mai ghi đè lại).
KB_TRENDS_TTL_SECONDS = 30 * 24 * 3600


def build_trends_summary(kb: dict) -> dict:
    """Tóm tắt kb.json thành top-N theo count mỗi mục (themes/companies/tech/
    deep_tech), dùng làm dữ liệu cho lệnh "/trends" và làm RAG context bổ
    sung cho free-text Q&A ở Worker (xem worker/src/index.js)."""

    def top_n(section: dict, n: int = KB_TRENDS_TOP_N) -> list[dict]:
        entries = [
            {
                "name": name,
                "count": v.get("count", 0),
                "last_seen": v.get("last_seen", ""),
            }
            for name, v in section.items()
        ]
        entries.sort(key=lambda x: x["count"], reverse=True)
        return entries[:n]

    return {
        "themes": top_n(kb.get("themes", {})),
        "companies": top_n(kb.get("companies", {})),
        "tech": top_n(kb.get("tech", {})),
        "deep_tech": top_n(kb.get("deep_tech", {})),
    }


def write_trends_to_kv(kb: dict) -> bool:
    """Ghi tóm tắt kb.json (build_trends_summary) lên Cloudflare KV key
    "trends:latest" để Worker trả lời lệnh "/trends" và làm RAG context.

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

    summary = build_trends_summary(kb)
    ok = _kv_put(
        key="trends:latest",
        value=json.dumps(summary, ensure_ascii=False),
        account_id=account_id,
        namespace_id=namespace_id,
        api_token=api_token,
        ttl_seconds=KB_TRENDS_TTL_SECONDS,
    )
    if ok:
        logger.info(
            "deliver.py: đã ghi 'trends:latest' (top %d mỗi mục) lên Cloudflare KV.",
            KB_TRENDS_TOP_N,
        )
    return ok


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
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.warning(
            "deliver.py: thiếu TELEGRAM_BOT_TOKEN hoặc TELEGRAM_CHAT_ID trong "
            "env -> không gửi được, in ra console thay thế (không crash)."
        )
        for chunk in chunks:
            print(chunk)
        return False

    if photo_url:
        send_telegram_photo(token, chat_id, photo_url, caption=format_header(now))

    all_ok = True
    last_idx = len(chunks) - 1
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == last_idx else None
        ok = send_telegram_message(token, chat_id, chunk, reply_markup=markup)
        all_ok = all_ok and ok

    return all_ok

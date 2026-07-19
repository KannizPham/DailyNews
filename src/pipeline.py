"""Item schema, Stage 1 lọc/xếp hạng tối đa 8 tin và Stage 2 tạo bản tin.

Bước 2+: pipeline.py phình thêm hàm khi build tiếp (đúng kế hoạch ban đầu),
không tách file mới cho mỗi stage.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sources.source_registry import get_display_name, get_tier

logger = logging.getLogger(__name__)

LEGACY_TYPES = {"research", "funding", "product", "deep_tech", "outside"}
DAILY_TYPES = {
    "macro",
    "markets",
    "banking",
    "corporate",
    "technology",
    "geopolitics",
}
VALID_TYPES = LEGACY_TYPES | DAILY_TYPES
DAILY_ITEM_LIMIT = 8
MAX_DAILY_ITEM_AGE_HOURS = 48
MAX_FUTURE_CLOCK_SKEW_HOURS = 6


@dataclass
class Item:
    """Đại diện chuẩn hoá cho mọi item từ mọi nguồn (§2 của brief).

    source_tier: mặc định = 3 (an toàn, "chưa uy tín tới khi được thêm vào
    bảng"). Việc tra bảng tier thật (source_registry.py) là bước 3 — ở bước 1
    field này chỉ là stub để các module sau gắn vào, KHÔNG implement registry.
    """

    id: str
    type: str
    title: str
    url: str
    source: str
    published_at: datetime
    raw_text: str = ""
    source_tier: int = 3
    score: float = 0.0

    def __post_init__(self) -> None:
        if self.type not in VALID_TYPES:
            raise ValueError(f"Item.type không hợp lệ: {self.type!r}")


def make_item_id(url: str, title: str) -> str:
    """Hash ổn định của url+title, dùng để dedupe qua state/seen.json."""
    key = f"{url}|{title}".strip().lower()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def hours_since(published_at: datetime, now: Optional[datetime] = None) -> float:
    now = now or datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    delta = now - published_at
    return delta.total_seconds() / 3600.0


@dataclass
class ThesisConfig:
    """Subset của thesis.yaml cần cho Stage 1 heuristic scoring + lựa mục outside."""

    tracking: list[str] = field(default_factory=list)
    deep_tech_tracking: list[str] = field(default_factory=list)
    keywords_boost: list[str] = field(default_factory=list)
    outside_lane_domains: list[str] = field(default_factory=list)
    category_priorities: dict[str, list[str]] = field(default_factory=dict)
    keywords_downrank: list[str] = field(default_factory=list)
    what_counts_as_matters: str = ""


def score_item(item: Item, thesis: ThesisConfig, now: Optional[datetime] = None) -> float:
    """Heuristic Stage 1 (rule-based, không gọi LLM):
    - khớp keyword trong thesis.tracking / deep_tech_tracking / keywords_boost
    - độ mới (item mới hơn -> điểm cao hơn, giảm dần trong 24h)
    - HN points (đã chấm điểm sẵn trong item.score lúc fetch, cộng dồn vào đây)
    - match category arXiv (đã phản ánh qua item.type == "research")

    Trả về điểm số càng cao càng ưu tiên giữ lại ở Stage 1.
    """
    text = f"{item.title} {item.raw_text}".lower()
    score = 0.0

    # Khớp keyword thesis — mỗi từ khoá khớp +2 điểm.
    category_keywords = [
        keyword
        for keywords in thesis.category_priorities.values()
        for keyword in keywords
    ]
    all_keywords = (
        thesis.tracking
        + thesis.deep_tech_tracking
        + thesis.keywords_boost
        + category_keywords
    )
    for kw in all_keywords:
        # so khớp từng từ trong cụm keyword (đơn giản, không cần NLP ở bước 1)
        kw_tokens = [t for t in kw.lower().replace("/", " ").split() if len(t) > 2]
        if kw_tokens and any(t in text for t in kw_tokens):
            score += 2.0

    for phrase in thesis.keywords_downrank:
        if phrase.lower() in text:
            score -= 2.5

    # Độ mới: tuyến tính giảm từ +5 (vừa đăng) về 0 (24h trước).
    age_h = hours_since(item.published_at, now)
    freshness = min(5.0, max(0.0, 5.0 * (1 - age_h / 24.0)))
    score += freshness

    if item.type in DAILY_TYPES:
        score += 1.0

    if item.type == "technology":
        economic_impact_terms = (
            "năng suất",
            "chi phí",
            "doanh thu",
            "lợi nhuận",
            "việc làm",
            "đầu tư",
            "bán dẫn",
            "semiconductor",
            "chuỗi cung ứng",
            "thị phần",
            "capex",
            "productivity",
            "revenue",
        )
        pure_launch_terms = (
            "launch",
            "released",
            "release",
            "new model",
            "chatbot",
            "benchmark",
            "leaderboard",
            "version",
        )
        has_economic_impact = any(term in text for term in economic_impact_terms)
        if has_economic_impact:
            score += 3.0
        elif any(term in text for term in pure_launch_terms):
            score -= 3.0

    source_tier = get_tier(item.source)
    if source_tier == 1:
        score += 1.5
    elif source_tier == 2:
        score += 0.75

    # HN points / nguồn đã chấm sẵn (item.score được set lúc fetch, ví dụ
    # points HN scale xuống). Cộng trực tiếp vào heuristic tổng.
    score += item.score

    return round(score, 3)


def dedupe_against_seen(items: list[Item], seen_ids: set[str]) -> list[Item]:
    """Bỏ item đã có trong state/seen.json (theo hash id)."""
    return [it for it in items if it.id not in seen_ids]


def filter_stage1(
    items: list[Item],
    thesis: ThesisConfig,
    seen_ids: Optional[set[str]] = None,
    keep_top_n: int = 20,
    now: Optional[datetime] = None,
) -> list[Item]:
    """Dedupe, chấm điểm và giữ một candidate pool cân bằng theo type.

    Giữ candidate theo từng type để nguồn công nghệ có lưu lượng lớn không
    loại sạch macro, markets, banking, corporate hoặc geopolitics trước khi
    áp dụng TARGET_ALLOCATION.
    """
    seen_ids = seen_ids or set()
    deduped = dedupe_against_seen(items, seen_ids)

    # Freshness là điều kiện bắt buộc, không chỉ là điểm thưởng. Nếu chỉ cho
    # bài mới thêm điểm thì một bài cũ có nhiều keyword/điểm nguồn vẫn có thể
    # lọt vào top và xuất hiện lại nhiều tuần sau. Cho phép tối đa 48 giờ để
    # không làm bản tin cuối tuần rỗng; timestamp tương lai quá 6 giờ cũng bị
    # loại vì thường là feed lỗi múi giờ/ngày.
    fresh_items: list[Item] = []
    for item in deduped:
        age_h = hours_since(item.published_at, now)
        if age_h > MAX_DAILY_ITEM_AGE_HOURS or age_h < -MAX_FUTURE_CLOCK_SKEW_HOURS:
            logger.info(
                "Stage 1 bỏ bài ngoài cửa sổ freshness: age=%.1fh source=%s title=%s",
                age_h,
                item.source,
                item.title[:100],
            )
            continue
        fresh_items.append(item)

    for it in fresh_items:
        it.score = score_item(it, thesis, now=now)

    by_type: dict[str, list[Item]] = {}
    for it in fresh_items:
        by_type.setdefault(it.type, []).append(it)

    result: list[Item] = []
    n_types = max(len(by_type), 1)
    # Tối thiểu 3/type để LLM còn có lựa chọn và loại được bài trùng sự kiện.
    per_type_n = max((keep_top_n + n_types - 1) // n_types, 3)

    for item_type, type_items in by_type.items():
        type_items.sort(key=lambda it: it.score, reverse=True)
        result.extend(type_items[:per_type_n])

    result.sort(key=lambda it: it.score, reverse=True)
    return result


# Cơ cấu daily tối đa 8 tin. Category cũ vẫn hợp lệ để giữ tương thích dữ liệu
# và tests, nhưng không có quota mặc định nên không thể lấn át bản tin kinh tế.
TARGET_ALLOCATION: dict[str, int] = {
    "macro": 2,
    "markets": 2,
    "banking": 1,
    "corporate": 1,
    "technology": 1,
    "geopolitics": 1,
}


def tag_outside_candidates(items: list, thesis: "ThesisConfig") -> list:
    """Không có nguồn nào tự nhiên emit type="outside" (§4 brief không định
    nghĩa fetcher riêng cho outside-lane, chỉ định nghĩa outside_lane_domains
    trong thesis.yaml như "chủ đề lệch khỏi thesis chính"). Quyết định kỹ
    thuật: tự retag item nào KHÔNG match thesis.tracking/deep_tech_tracking
    nhưng CÓ match outside_lane_domains thành type="outside" (copy, không sửa
    item gốc, để không phá vỡ phân loại research/funding/product/deep_tech
    dùng trong các bước khác).

    Nếu không tìm thấy ứng viên nào match outside_lane_domains, vẫn không bịa
    type — Stage 1.5/2 sẽ tự nói "không có ứng viên outside" qua allocation
    rỗng (đúng yêu cầu thà thiếu còn hơn ép tin không liên quan).
    """
    if not thesis.outside_lane_domains:
        return items

    import copy as _copy

    outside_keywords = []
    for kw in thesis.outside_lane_domains:
        tokens = [t for t in kw.lower().replace("/", " ").split() if len(t) > 2]
        outside_keywords.extend(tokens)

    thesis_keywords = []
    for kw in thesis.tracking + thesis.deep_tech_tracking:
        tokens = [t for t in kw.lower().replace("/", " ").split() if len(t) > 2]
        thesis_keywords.extend(tokens)

    tagged = []
    for it in items:
        text = f"{it.title} {it.raw_text}".lower()
        matches_outside = any(kw in text for kw in outside_keywords)
        matches_thesis = any(kw in text for kw in thesis_keywords)

        if matches_outside and not matches_thesis and it.type != "deep_tech":
            outside_copy = _copy.copy(it)
            outside_copy.type = "outside"
            tagged.append(outside_copy)
        else:
            tagged.append(it)

    return tagged


_EVENT_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "after", "into",
    "của", "và", "cho", "với", "trong", "sau", "trước", "đang", "được",
    "một", "những", "các", "khi", "về", "tại", "trên", "theo", "mới",
}


def _event_tokens(item: Item) -> set[str]:
    tokens = re.findall(r"[0-9a-zA-ZÀ-ỹà-ỹ]+", item.title.lower())
    return {
        token
        for token in tokens
        if len(token) > 2 and token not in _EVENT_STOPWORDS
    }


def is_same_event(first: Item, second: Item) -> bool:
    """Nhận diện gần-trùng xác định bằng title, không gọi LLM và không đổi URL."""
    if first.id == second.id or first.url == second.url:
        return True
    first_tokens = _event_tokens(first)
    second_tokens = _event_tokens(second)
    if not first_tokens or not second_tokens:
        return False
    shared = first_tokens & second_tokens
    similarity = len(shared) / len(first_tokens | second_tokens)
    return len(shared) >= 3 and similarity >= 0.45


def _append_distinct(selected: list[Item], candidate: Item) -> bool:
    if len(selected) >= DAILY_ITEM_LIMIT:
        return False
    if any(is_same_event(candidate, existing) for existing in selected):
        return False
    selected.append(candidate)
    return True


def _select_by_allocation(
    items: list, allocation: dict[str, int]
) -> list:
    """Chọn item theo đúng cơ cấu type, ưu tiên score cao nhất trong từng type.

    Dùng làm fallback khi không có LLM (heuristic thuần) VÀ làm bước chốt
    sau khi LLM xếp hạng lại thứ tự ưu tiên trong từng type.
    """
    selected = []
    by_type: dict[str, list] = {}
    for it in items:
        by_type.setdefault(it.type, []).append(it)
    for t in by_type:
        by_type[t].sort(key=lambda it: it.score, reverse=True)

    for item_type, count in allocation.items():
        candidates = by_type.get(item_type, [])
        chosen = 0
        for candidate in candidates:
            if _append_distinct(selected, candidate):
                chosen += 1
            if chosen >= count or len(selected) >= DAILY_ITEM_LIMIT:
                break

    return selected[:DAILY_ITEM_LIMIT]


def _build_ranking_prompt(items: list, thesis: "ThesisConfig") -> str:
    lines = [
        "Đây là thesis của operator (lăng kính để đánh giá độ liên quan):",
        f"- tracking: {', '.join(thesis.tracking)}",
        f"- deep_tech_tracking: {', '.join(thesis.deep_tech_tracking)}",
        f"- keywords_boost: {', '.join(thesis.keywords_boost)}",
        f"- category_priorities: {json.dumps(thesis.category_priorities, ensure_ascii=False)}",
        f"- điều được xem là quan trọng: {thesis.what_counts_as_matters}",
        "",
        "Dưới đây là danh sách item đã fetch, mỗi item có id số thứ tự, type, "
        "title, source và tuổi tin. Hãy xếp hạng trong từng type, ưu tiên tin "
        "mới trong 24 giờ, nguồn đáng tin và tác động kinh tế cụ thể. Không xếp "
        "nhiều bài về cùng một sự kiện. Với technology, hạ mạnh launch/model "
        "release thuần túy nếu không có tác động tới năng suất, chi phí, doanh "
        "nghiệp, thị trường hoặc chuỗi cung ứng. Trả về DUY NHẤT JSON dạng "
        '{"ranked_ids": {"macro": [id,...], "markets": [id,...], '
        '"banking": [id,...], "corporate": [id,...], "technology": [id,...], '
        '"geopolitics": [id,...]}}. Không đổi type, không thêm chữ ngoài JSON.',
        "",
    ]
    for idx, it in enumerate(items):
        age_h = max(0.0, hours_since(it.published_at))
        lines.append(
            f"id={idx} type={it.type} source={it.source} "
            f"age_hours={age_h:.1f} title={it.title}"
        )
    return "\n".join(lines)


def llm_rank_and_select(
    items: list,
    thesis: "ThesisConfig",
    llm_client,
    allocation: dict[str, int] = TARGET_ALLOCATION,
) -> list:
    """Gọi LLM 1 lần (batch) để xếp hạng lại độ liên quan trong từng type, sau
    đó chọn theo cơ cấu allocation. Nếu LLM lỗi/parse fail -> fallback về
    heuristic score thuần (_select_by_allocation trên item.score gốc).

    Đây là phần "tăng dần, fallback an toàn" theo yêu cầu: pipeline KHÔNG được
    crash chỉ vì thiếu API key — fallback graceful, log rõ.
    """
    if llm_client is None:
        logger.warning(
            "llm_rank_and_select: không có llm_client (thiếu key) -> fallback "
            "heuristic score thuần, không gọi LLM batch ranking."
        )
        return _select_by_allocation(items, allocation)

    try:
        prompt = _build_ranking_prompt(items, thesis)
        response = llm_client.generate(prompt)
        logger.info(
            "Stage 1 LLM ranking [%s]: input_tokens=%s output_tokens=%s",
            response.engine,
            response.input_tokens,
            response.output_tokens,
        )
        text = response.text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        parsed = json.loads(text)
        ranked_ids = parsed["ranked_ids"]

        selected = []
        for item_type, count in allocation.items():
            if len(selected) >= DAILY_ITEM_LIMIT:
                break
            ids_for_type = ranked_ids.get(item_type, [])
            chosen = 0
            for idx in ids_for_type:
                if not isinstance(idx, int) or idx < 0 or idx >= len(items):
                    continue
                if items[idx].type != item_type:
                    continue
                if _append_distinct(selected, items[idx]):
                    chosen += 1
                if chosen >= count or len(selected) >= DAILY_ITEM_LIMIT:
                    break
            if chosen < count:
                # LLM không trả đủ id cho type này -> bù bằng heuristic score.
                by_type_fallback = [it for it in items if it.type == item_type]
                by_type_fallback.sort(key=lambda it: it.score, reverse=True)
                already_ids = {it.id for it in selected}
                for it in by_type_fallback:
                    if it.id in already_ids:
                        continue
                    if _append_distinct(selected, it):
                        chosen += 1
                    if chosen >= count or len(selected) >= DAILY_ITEM_LIMIT:
                        break
        return selected[:DAILY_ITEM_LIMIT]
    except Exception as exc:  # noqa: BLE001 - LLM lỗi không được sập Stage 1
        logger.warning(
            "llm_rank_and_select: LLM batch ranking lỗi (%s) -> fallback heuristic "
            "score thuần.",
            exc,
        )
        return _select_by_allocation(items, allocation)


def build_kb_summary(kb: dict, top_n: int = 5) -> str:
    """Tóm tắt knowledge base hiện tại (top themes/investors/tech) để feed vào
    Stage 2 prompt — đúng §5: "tóm tắt knowledge base hiện tại (top themes/
    investors/tech đang nổi, lấy từ kb.json)"."""

    def _top(category: dict, n: int) -> list[str]:
        ranked = sorted(category.items(), key=lambda kv: kv[1].get("count", 0), reverse=True)
        return [f"{name} (x{info.get('count', 0)})" for name, info in ranked[:n]]

    parts = []
    themes = _top(kb.get("themes", {}), top_n)
    companies = _top(kb.get("companies", {}), top_n)
    investors = _top(kb.get("investors", {}), top_n)
    tech = _top(kb.get("tech", {}), top_n)
    deep_tech = _top(kb.get("deep_tech", {}), top_n)

    if themes:
        parts.append(f"Themes đang nổi: {', '.join(themes)}")
    if companies:
        parts.append(f"Companies đã thấy nhiều lần: {', '.join(companies)}")
    if investors:
        parts.append(f"Investors đã thấy nhiều lần: {', '.join(investors)}")
    if tech:
        parts.append(f"Tech đang lặp lại: {', '.join(tech)}")
    if deep_tech:
        parts.append(f"Deep tech/nguyên lý đang lặp lại: {', '.join(deep_tech)}")

    return "\n".join(parts) if parts else "(Knowledge base còn rỗng, chưa có pattern tích luỹ.)"


def _build_analysis_prompt(
    verified_items: list, thesis: "ThesisConfig", kb_summary: str
) -> str:
    """Build user prompt cho Stage 2 — system prompt riêng (prompts.py),
    đây là phần data: thesis + tối đa 8 item đã verify + kb summary."""
    lines = [
        "=== THESIS (lăng kính phân tích) ===",
        f"tracking: {', '.join(thesis.tracking)}",
        f"deep_tech_tracking: {', '.join(thesis.deep_tech_tracking)}",
        f"keywords_boost: {', '.join(thesis.keywords_boost)}",
        f"what_counts_as_matters: {thesis.what_counts_as_matters}",
        "",
        "=== KNOWLEDGE BASE HIỆN TẠI (pattern đang tích luỹ) ===",
        kb_summary,
        "",
        "=== ITEM ĐÃ CHỌN (kèm nhãn confidence đã gắn sẵn từ verify.py — DÙNG "
        "LẠI nhãn này, KHÔNG tự đổi) ===",
    ]
    for v in verified_items:
        it = v.item
        # Tính sẵn tên nguồn đẹp (vd "techcrunch.com" -> "TechCrunch") bằng
        # code, KHÔNG để LLM tự "dịch" — LLM hay lười và giữ nguyên domain thô
        # dù prompt đã cho ví dụ (xem source_registry.get_display_name).
        source_display = get_display_name(it.source)
        lines.append(
            f"\n[type={it.type}] confidence={v.confidence_emoji} "
            f"(tier={v.source_tier}, cross_confirmed={v.cross_confirmed})\n"
            f"Title: {it.title}\nSource display name (DÙNG ĐÚNG TÊN NÀY khi "
            f"trích dẫn, không tự đổi): {source_display}\nURL: {it.url}\n"
            f"Nội dung: {it.raw_text[:1500]}"
        )
    return "\n".join(lines)


@dataclass
class AnalysisResult:
    digest_text: str
    kb_update: Optional[dict] = None
    engine: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    json_parse_error: Optional[str] = None


def _split_digest_and_json(raw_text: str) -> tuple[str, Optional[dict], Optional[str]]:
    """Tách digest markdown và JSON block cuối (sau dòng '---JSON---').

    Parse an toàn (strip ```), nếu parse fail vẫn trả digest, kèm lỗi để log
    (đúng §5: "Parse JSON an toàn (strip ```), nếu parse fail thì vẫn gửi
    digest, log lỗi.")
    """
    marker = "---JSON---"
    if marker not in raw_text:
        return raw_text.strip(), None, "Không tìm thấy marker ---JSON--- trong response"

    digest_part, _, json_part = raw_text.partition(marker)
    digest_text = digest_part.strip()
    json_text = json_part.strip()

    if json_text.startswith("```"):
        json_text = json_text.strip("`")
        if json_text.lower().startswith("json"):
            json_text = json_text[4:].strip()

    try:
        kb_update = json.loads(json_text)
        return digest_text, kb_update, None
    except json.JSONDecodeError as exc:
        return digest_text, None, f"JSON parse lỗi: {exc}"


def analyze_stage2(
    verified_items: list,
    thesis: "ThesisConfig",
    kb_summary: str,
    llm_client,
    system_prompt: str,
) -> AnalysisResult:
    """Stage 2 — gọi LLM một lần với tối đa 8 item + thesis + kb_summary.

    Nếu llm_client là None (thiếu cả GEMINI_API_KEY và DEEPSEEK_API_KEY) ->
    trả AnalysisResult với digest_text là thông báo rõ ràng "thiếu key", KHÔNG
    crash pipeline (yêu cầu: degrade gracefully).
    """
    if llm_client is None:
        msg = (
            "[Stage 2 KHÔNG chạy được] Thiếu GEMINI_API_KEY và DEEPSEEK_API_KEY "
            "trong biến môi trường — không thể gọi LLM để phân tích. Đã fetch + "
            "filter + verify thành công, nhưng digest kinh tế cần ít nhất 1 "
            "trong 2 key trên. Thêm key vào .env hoặc GitHub Secrets rồi chạy lại."
        )
        logger.warning(msg)
        return AnalysisResult(digest_text=msg, kb_update=None, json_parse_error="missing_llm_key")

    prompt = _build_analysis_prompt(verified_items, thesis, kb_summary)
    try:
        response = llm_client.generate(prompt, system=system_prompt)
    except Exception as exc:  # noqa: BLE001 - Stage 2 lỗi không được sập toàn pipeline
        msg = f"[Stage 2 LỖI] Gọi LLM thất bại: {exc}"
        logger.error(msg)
        return AnalysisResult(digest_text=msg, kb_update=None, json_parse_error=str(exc))

    logger.info(
        "Stage 2 analyze [%s]: input_tokens=%s output_tokens=%s (log để kiểm "
        "chứng chi phí ~$0)",
        response.engine,
        response.input_tokens,
        response.output_tokens,
    )

    digest_text, kb_update, parse_error = _split_digest_and_json(response.text)
    if parse_error:
        logger.warning("Stage 2: %s", parse_error)

    return AnalysisResult(
        digest_text=digest_text,
        kb_update=kb_update,
        engine=response.engine,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        json_parse_error=parse_error,
    )

"""Stage 1.5 — verify tự động (§6b của brief).

Tính `confidence` cho mỗi item bằng code if/else thuần (KHÔNG hỏi LLM quyết
định mức tin cậy) theo quy tắc cứng:
  - cross_confirmed: tìm trong cùng batch item đã fetch ở Stage 0 xem có item
    khác (nguồn khác domain) nói cùng sự kiện không (so khớp token tiêu đề
    đơn giản — không cần LLM, không cần gọi web-search thật vì pipeline brief
    chỉ yêu cầu "tự thử tìm thêm... trong các item đã fetch ở Stage 0").
  - 🟢 confirmed: tier 1-2 VÀ (cross_confirmed=true HOẶC claim có số liệu trích
    trực tiếp kèm link).
  - 🟡 reasoned: là suy luận/phân tích (không phải fact trích xuất), dựa trên
    dữ kiện tier 1-2.
  - 🔴 single_source: tier 3 (mặc định nếu không cross-confirm), hoặc tier 1-2
    không cross-confirm được, hoặc suy luận dựa trên dữ liệu tier 3.

Quyết định khi spec mơ hồ: "cross-reference" triển khai bằng so khớp từ khoá
giữa các item Stage 0 cùng ngày (không gọi LLM/web-search thật, vì brief cho
phép "hoặc 1 lần gọi LLM dùng web-search-style query NẾU pipeline có quyền
truy cập search" — pipeline này KHÔNG có search tool, nên dùng nhánh đầu:
tìm trong các item đã fetch). Cách này rẻ ($0), xác định được (không phụ
thuộc LLM), và đúng tinh thần "không giao cho LLM tự quyết".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from pipeline import Item
from sources.source_registry import get_tier

logger = logging.getLogger(__name__)

CONFIRMED = "confirmed"
REASONED = "reasoned"
SINGLE_SOURCE = "single_source"

CONFIDENCE_EMOJI = {
    CONFIRMED: "🟢",
    REASONED: "🟡",
    SINGLE_SOURCE: "🔴",
}

# Regex nhận diện claim "có số liệu cụ thể" (tiền, %, ngày) — dùng để quyết
# định item có cần trích nguồn/link cụ thể mới được 🟢 hay không (§6b).
NUMERIC_CLAIM_PATTERN = re.compile(
    r"(\$\s?\d|[\d,.]+\s?(nghìn|triệu|tỷ|billion|million|m\b|b\b|đồng)|%|"
    r"\bUSD\b|\bVND\b|\bbps?\b|điểm\s+cơ\s+bản)",
    re.IGNORECASE,
)

# Stopword tiếng Anh/Việt ngắn, loại khỏi so khớp token để cross-reference
# không bị "khớp giả" vì các từ phổ biến (the, a, của, và...).
STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are",
    "with", "at", "by", "from", "as", "this", "that", "it", "its",
    "của", "và", "là", "cho", "với", "các", "một", "này", "có", "trong",
    "sau", "trước", "đang", "được", "theo", "tại", "trên", "khi", "về",
}


@dataclass
class VerifiedItem:
    """Item kèm kết quả verify — Stage 2 PHẢI dùng confidence này, không tự gắn."""

    item: Item
    source_tier: int
    cross_confirmed: bool
    has_numeric_claim: bool
    confidence: str  # CONFIRMED | REASONED | SINGLE_SOURCE
    confidence_emoji: str
    confirming_sources: list[str] = field(default_factory=list)

    def label(self) -> str:
        """Nhãn ngắn để chèn ngay sau claim trong digest, ví dụ '🟢'."""
        return self.confidence_emoji


def _title_tokens(title: str) -> set[str]:
    tokens = re.findall(r"[a-zA-ZÀ-ỹà-ỹ0-9]+", title.lower())
    return {t for t in tokens if len(t) > 2 and t not in STOPWORDS}


def _has_numeric_claim(text: str) -> bool:
    return bool(NUMERIC_CLAIM_PATTERN.search(text))


def find_cross_confirmation(
    item: Item, all_items: list[Item], min_shared_tokens: int = 3
) -> list[str]:
    """Tìm item khác (domain khác) trong batch Stage 0 nói cùng sự kiện.

    So khớp bằng số token tiêu đề trùng nhau (đơn giản, rẻ, xác định được —
    không gọi LLM). Trả về list domain nguồn xác nhận thêm (rỗng nếu không
    tìm thấy).
    """
    base_tokens = _title_tokens(item.title)
    if len(base_tokens) < min_shared_tokens:
        return []

    confirming: list[str] = []
    for other in all_items:
        if other.id == item.id or other.source == item.source:
            continue
        other_tokens = _title_tokens(other.title)
        shared = base_tokens & other_tokens
        if len(shared) >= min_shared_tokens:
            confirming.append(other.source)

    return confirming


def compute_confidence(
    source_tier: int,
    cross_confirmed: bool,
    has_numeric_claim: bool,
    is_reasoning: bool = False,
) -> str:
    """Quy tắc cứng §6b, thuần if/else — không giao cho LLM.

    is_reasoning=True nghĩa là claim này là suy luận/phân tích (vd "Lăng kính
    tiền - Suy luận của tôi") chứ không phải fact trích xuất trực tiếp.
    """
    # Claim số liệu cụ thể bắt buộc trích được nguồn/link cụ thể (ở đây: phải
    # đến từ tier 1-2 hoặc cross-confirm) -> nếu không, ép 🔴 dù tier nào.
    if has_numeric_claim and not is_reasoning:
        if source_tier in (1, 2):
            # Claim số liệu từ nguồn tier 1-2 có link cụ thể trong raw_text
            # (đã được fetch trực tiếp từ nguồn đó) -> đạt điều kiện 🟢 nếu
            # cross_confirmed HOẶC là số liệu trích trực tiếp có link (chính
            # raw_text từ tier 1-2 coi như "trích trực tiếp có link" vì item
            # luôn có item.url).
            return CONFIRMED
        return SINGLE_SOURCE

    if is_reasoning:
        # Suy luận luôn 🟡 hoặc 🔴, KHÔNG BAO GIỜ 🟢 (§7 ANALYSIS_SYSTEM_PROMPT).
        if source_tier in (1, 2):
            return REASONED
        return SINGLE_SOURCE

    # Claim thường (không số liệu, không phải suy luận) — fact trích xuất.
    if source_tier in (1, 2) and cross_confirmed:
        return CONFIRMED
    if source_tier in (1, 2) and not cross_confirmed:
        # tier 1-2 nhưng không cross-confirm được -> không đủ để 🟢.
        return SINGLE_SOURCE
    # tier 3 không cross-confirm -> ép 🔴, không có ngoại lệ (§6b).
    if source_tier == 3 and cross_confirmed:
        return REASONED if is_reasoning else SINGLE_SOURCE
    return SINGLE_SOURCE


def verify_item(item: Item, all_items: list[Item]) -> VerifiedItem:
    """Verify 1 item: tra tier, cross-reference, tính confidence theo quy tắc cứng."""
    tier = get_tier(item.source)
    confirming_sources = find_cross_confirmation(item, all_items)
    cross_confirmed = len(confirming_sources) > 0
    has_numeric = _has_numeric_claim(item.raw_text) or _has_numeric_claim(item.title)

    confidence = compute_confidence(
        source_tier=tier,
        cross_confirmed=cross_confirmed,
        has_numeric_claim=has_numeric,
        is_reasoning=False,
    )

    # Item tier 3 không cross-confirm -> ép 🔴, không có ngoại lệ (§6b, đảm
    # bảo bất biến này luôn đúng dù logic compute_confidence ở trên thay đổi).
    if tier == 3 and not cross_confirmed:
        confidence = SINGLE_SOURCE

    return VerifiedItem(
        item=item,
        source_tier=tier,
        cross_confirmed=cross_confirmed,
        has_numeric_claim=has_numeric,
        confidence=confidence,
        confidence_emoji=CONFIDENCE_EMOJI[confidence],
        confirming_sources=confirming_sources,
    )


def verify_stage(selected_items: list[Item], all_fetched_items: list[Item]) -> list[VerifiedItem]:
    """Stage 1.5 đầy đủ: verify từng item đã chọn ở Stage 1, cross-reference
    trong TOÀN BỘ item đã fetch ở Stage 0 (không chỉ trong selected_items, vì
    cross-reference cần tìm trong toàn bộ tin đã có, không chỉ 6 tin được chọn).
    """
    verified = []
    for item in selected_items:
        try:
            verified.append(verify_item(item, all_fetched_items))
        except Exception as exc:  # noqa: BLE001 - 1 item verify lỗi không sập stage
            logger.warning("verify.py: lỗi verify item %s, ép single_source: %s", item.id, exc)
            verified.append(
                VerifiedItem(
                    item=item,
                    source_tier=get_tier(item.source),
                    cross_confirmed=False,
                    has_numeric_claim=False,
                    confidence=SINGLE_SOURCE,
                    confidence_emoji=CONFIDENCE_EMOJI[SINGLE_SOURCE],
                )
            )
    return verified

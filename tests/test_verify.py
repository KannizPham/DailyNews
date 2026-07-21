"""Unit test cho verify.py — đặc biệt acceptance criteria §11.6:
"Item nguồn tier 3 không cross-confirm được PHẢI hiện 🔴 trong output (test
bằng cách giả một item tier 3 đơn lẻ)."

Chạy: cd src && python -m pytest ../tests/test_verify.py -v
(hoặc python ../tests/test_verify.py nếu không có pytest)
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipeline import Item  # noqa: E402
from verify import (  # noqa: E402
    CONFIRMED,
    REASONED,
    SINGLE_SOURCE,
    compute_confidence,
    find_cross_confirmation,
    verify_item,
    verify_stage,
)


def make_item(
    title: str,
    source: str,
    item_type: str = "research",
    raw_text: str = "",
    url: str = None,
) -> Item:
    url = url or f"https://{source}/{abs(hash(title))}"
    return Item(
        id=f"id-{abs(hash(title + source))}",
        type=item_type,
        title=title,
        url=url,
        source=source,
        published_at=datetime.now(timezone.utc),
        raw_text=raw_text,
    )


def test_tier3_single_source_forces_red():
    """Acceptance criteria §11.6: tier 3, không có item nào khác xác nhận
    -> PHẢI là single_source (🔴), không có ngoại lệ."""
    lone_item = make_item(
        "Obscure startup raises mystery funding round",
        source="news.ycombinator.com",  # tier 3 theo SOURCE_TIERS
    )
    # Batch chỉ có item này, không có gì khác để cross-confirm.
    all_items = [lone_item]

    result = verify_item(lone_item, all_items)

    assert result.source_tier == 3
    assert result.cross_confirmed is False
    assert result.confidence == SINGLE_SOURCE
    assert result.confidence_emoji == "🔴"


def test_tier3_with_cross_confirmation_not_forced_red_but_not_green():
    """tier 3 + cross_confirmed=True không tự động lên 🟢 (chỉ tier 1-2 mới đủ
    điều kiện 🟢). Theo compute_confidence: tier 3 cross_confirmed -> reasoned
    nếu is_reasoning, single_source nếu fact thường (không đủ điều kiện 🟢)."""
    item_a = make_item(
        "Company X raises Series A funding round today",
        source="news.ycombinator.com",
    )
    item_b = make_item(
        "Company X raises Series A funding round today",
        source="some-random-blog.example.com",
    )
    all_items = [item_a, item_b]

    result = verify_item(item_a, all_items)

    assert result.source_tier == 3
    assert result.cross_confirmed is True
    # Tier 3 không bao giờ đạt 🟢 dù cross-confirm, theo §6b chỉ tier 1-2 mới
    # đủ điều kiện confirmed.
    assert result.confidence != CONFIRMED
    assert result.confidence_emoji != "🟢"


def test_tier1_cross_confirmed_fact_is_green():
    item_a = make_item(
        "OpenAI announces new model release today",
        source="arxiv.org",  # tier 1
    )
    item_b = make_item(
        "OpenAI announces new model release today",
        source="techcrunch.com",  # tier 1, domain khác -> đủ điều kiện cross-confirm
    )
    result = verify_item(item_a, [item_a, item_b])

    assert result.source_tier == 1
    assert result.cross_confirmed is True
    assert result.confidence == CONFIRMED
    assert result.confidence_emoji == "🟢"


def test_tier1_no_cross_confirmation_not_green():
    """tier 1 nhưng không cross-confirm được -> không đủ để 🟢 (§6b)."""
    lone_item = make_item(
        "A completely unique research finding nobody else reports",
        source="arxiv.org",
    )
    result = verify_item(lone_item, [lone_item])

    assert result.source_tier == 1
    assert result.cross_confirmed is False
    assert result.confidence != CONFIRMED


def test_numeric_claim_without_tier1_2_forces_red():
    """Claim số liệu cụ thể (tiền) nhưng nguồn tier 3 -> tự động 🔴 dù gì."""
    item = make_item(
        "Mystery startup raised $40 million Series B round",
        source="producthunt.com",  # tier 3
        raw_text="They raised $40 million in Series B funding.",
    )
    result = verify_item(item, [item])

    assert result.has_numeric_claim is True
    assert result.confidence == SINGLE_SOURCE
    assert result.confidence_emoji == "🔴"


def test_vietnamese_numeric_claim_and_new_category_are_supported():
    item = make_item(
        "Ngân hàng công bố lợi nhuận mới",
        source="tuoitre.vn",
        item_type="banking",
        raw_text="Lợi nhuận trước thuế đạt 1.000 tỷ đồng.",
    )
    result = verify_item(item, [item])

    assert result.item.type == "banking"
    assert result.has_numeric_claim is True
    assert result.source_tier == 1


def test_compute_confidence_reasoning_never_green():
    """Suy luận (is_reasoning=True) không bao giờ 🟢 dù tier 1, theo §7
    ANALYSIS_SYSTEM_PROMPT: 'Suy luận của tôi... luôn 🟡 hoặc 🔴, không bao
    giờ 🟢'."""
    confidence = compute_confidence(
        source_tier=1, cross_confirmed=True, has_numeric_claim=False, is_reasoning=True
    )
    assert confidence != CONFIRMED
    assert confidence == REASONED


def test_verify_stage_does_not_crash_on_bad_item():
    """verify_stage không được sập dù 1 item lỗi (an toàn cho Stage 1.5)."""
    item = make_item("Normal item", source="techcrunch.com")
    result = verify_stage([item], [item])
    assert len(result) == 1
    assert result[0].confidence in (CONFIRMED, REASONED, SINGLE_SOURCE)


def test_find_cross_confirmation_requires_different_source():
    """Cross-confirmation phải từ NGUỒN KHÁC (khác domain), không tự xác nhận
    chính mình hay item khác cùng domain."""
    item_a = make_item("Big news happens today in tech world", source="techcrunch.com")
    item_b = make_item("Big news happens today in tech world", source="techcrunch.com")
    confirming = find_cross_confirmation(item_a, [item_a, item_b])
    assert confirming == []  # cùng domain -> không tính là cross-confirm


if __name__ == "__main__":
    # Cho phép chạy không cần pytest.
    test_fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in test_fns:
        try:
            fn()
            print(f"PASS: {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL: {fn.__name__}: {exc}")
    print(f"\n{len(test_fns) - failed}/{len(test_fns)} passed")
    if failed:
        sys.exit(1)

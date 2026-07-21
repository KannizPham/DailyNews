"""Tests cho category kinh tế, allocation và chống trùng sự kiện."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pipeline import (  # noqa: E402
    DAILY_ITEM_LIMIT,
    DAILY_TYPES,
    TARGET_ALLOCATION,
    VALID_TYPES,
    Item,
    ThesisConfig,
    _select_by_allocation,
    is_same_event,
    score_item,
)


def make_item(item_id: str, item_type: str, title: str, raw_text: str = "") -> Item:
    return Item(
        id=item_id,
        type=item_type,
        title=title,
        url=f"https://tuoitre.vn/{item_id}.htm",
        source="tuoitre.vn",
        published_at=datetime.now(timezone.utc),
        raw_text=raw_text,
    )


def test_daily_types_are_valid_and_allocation_is_capped_at_eight():
    assert DAILY_TYPES <= VALID_TYPES
    assert TARGET_ALLOCATION == {
        "macro": 2,
        "markets": 2,
        "banking": 1,
        "corporate": 1,
        "technology": 1,
        "geopolitics": 1,
    }
    assert sum(TARGET_ALLOCATION.values()) == DAILY_ITEM_LIMIT == 8


def test_selection_does_not_repeat_the_same_event():
    items = [
        make_item("macro-1", "macro", "Fed giữ nguyên lãi suất trong tháng 7"),
        make_item("macro-2", "macro", "Việt Nam công bố số liệu CPI tháng 7"),
        make_item("market-dup", "markets", "Tháng 7 Fed giữ nguyên lãi suất"),
        make_item("market-2", "markets", "VN-Index tăng nhờ nhóm ngân hàng"),
    ]
    for index, item in enumerate(items):
        item.score = 10 - index

    selected = _select_by_allocation(
        items,
        {"macro": 2, "markets": 2},
    )

    assert len(selected) == 3
    assert not any(
        is_same_event(first, second)
        for index, first in enumerate(selected)
        for second in selected[index + 1 :]
    )


def test_economic_technology_beats_pure_model_launch():
    thesis = ThesisConfig()
    launch = make_item(
        "launch",
        "technology",
        "New chatbot model launch tops benchmark leaderboard",
    )
    impact = make_item(
        "impact",
        "technology",
        "Semiconductor capex expands supply chain capacity",
        "Khoản đầu tư giúp giảm chi phí và tăng năng suất.",
    )

    assert score_item(impact, thesis) > score_item(launch, thesis)

"""Unit test cho deliver.py — phần tương tác real-time (inline keyboard +
ghi Cloudflare KV). Test trọng tâm: graceful degrade khi thiếu secret
Cloudflare (KHÔNG raise, KHÔNG sập), và build_inline_keyboard tạo đúng
callback_data khớp index dùng trong write_items_to_kv.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deliver import (  # noqa: E402
    build_inline_keyboard,
    build_trends_summary,
    write_items_to_kv,
    write_trends_to_kv,
)


@dataclass
class FakeItem:
    title: str
    url: str
    raw_text: str
    type: str


@dataclass
class FakeVerifiedItem:
    item: FakeItem
    confidence_emoji: str
    confidence: str = "confirmed"


def _make_verified_items(n: int) -> list[FakeVerifiedItem]:
    items = []
    for i in range(n):
        items.append(
            FakeVerifiedItem(
                item=FakeItem(
                    title=f"Tiêu đề item số {i + 1} dài dòng để test cắt chuỗi",
                    url=f"https://example.com/{i + 1}",
                    raw_text="raw text " * 300,  # > KV_RAW_TEXT_MAX_LEN
                    type="research",
                ),
                confidence_emoji="🟢",
            )
        )
    return items


def test_build_inline_keyboard_empty_returns_none():
    assert build_inline_keyboard([]) is None


def test_build_inline_keyboard_callback_data_matches_index():
    items = _make_verified_items(3)
    keyboard = build_inline_keyboard(items)

    assert keyboard is not None
    buttons = keyboard["inline_keyboard"]
    assert len(buttons) == 3
    # callback_data phải là "qa:<idx 1-based>", khớp đúng key "item:<idx>"
    # mà write_items_to_kv ghi vào KV.
    assert buttons[0][0]["callback_data"] == "qa:1"
    assert buttons[1][0]["callback_data"] == "qa:2"
    assert buttons[2][0]["callback_data"] == "qa:3"
    assert "🔍 Hỏi sâu thêm" in buttons[0][0]["text"]


def test_write_items_to_kv_graceful_degrade_when_missing_secrets(monkeypatch):
    """Thiếu CLOUDFLARE_API_TOKEN/ACCOUNT_ID/NAMESPACE_ID -> trả False, log
    warning, KHÔNG raise exception, KHÔNG gọi network."""
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_KV_NAMESPACE_ID", raising=False)

    items = _make_verified_items(2)
    result = write_items_to_kv(items)  # không raise là pass

    assert result is False


def test_write_items_to_kv_empty_list_returns_false(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "fake-token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "fake-account")
    monkeypatch.setenv("CLOUDFLARE_KV_NAMESPACE_ID", "fake-namespace")

    assert write_items_to_kv([]) is False


def test_build_trends_summary_sorts_by_count_desc_and_caps_top_n():
    kb = {
        "themes": {
            "A": {"count": 1, "last_seen": "2026-06-19"},
            "B": {"count": 5, "last_seen": "2026-06-20"},
            "C": {"count": 3, "last_seen": "2026-06-21"},
        },
        "companies": {},
        "tech": {},
        "deep_tech": {},
    }

    summary = build_trends_summary(kb)

    assert [e["name"] for e in summary["themes"]] == ["B", "C", "A"]
    assert summary["themes"][0] == {"name": "B", "count": 5, "last_seen": "2026-06-20"}
    assert summary["companies"] == []


def test_write_trends_to_kv_graceful_degrade_when_missing_secrets(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_KV_NAMESPACE_ID", raising=False)

    kb = {"themes": {"X": {"count": 1, "last_seen": "2026-06-19"}}}
    result = write_trends_to_kv(kb)  # không raise là pass

    assert result is False

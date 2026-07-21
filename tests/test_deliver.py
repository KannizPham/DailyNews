"""Unit test cho deliver.py — phần tương tác real-time (inline keyboard +
ghi Cloudflare KV). Test trọng tâm: graceful degrade khi thiếu secret
Cloudflare (KHÔNG raise, KHÔNG sập), và build_inline_keyboard tạo đúng
callback_data khớp index dùng trong write_items_to_kv.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deliver import (  # noqa: E402
    _kv_put,
    build_inline_keyboard,
    build_trends_summary,
    deliver,
    get_telegram_chat_ids,
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


def _trend(summary: dict, topic_id: str) -> dict:
    return next(item for item in summary["trends"] if item["topic_id"] == topic_id)


def test_trends_repeated_keyword_in_one_digest_counts_once():
    documents = [
        ("2026-07-19", "VN-Index tăng. VN-Index lập đỉnh; Vietnam stocks tiếp tục tăng."),
        ("2026-07-18", "Vietnamese equities ghi nhận thanh khoản cao."),
    ]

    summary = build_trends_summary(documents, as_of=date(2026, 7, 19))

    assert _trend(summary, "vietnam_stock_market")["count"] == 2


def test_old_ai_taxonomy_no_longer_appears():
    documents = [
        (
            "2026-07-19",
            "AI Application Layer, Agentic Infrastructure, Anthropic, OpenAI, LLM và AI Agents.",
        )
    ]

    summary = build_trends_summary(documents, as_of=date(2026, 7, 19))
    rendered = json.dumps(summary, ensure_ascii=False)

    for old_label in (
        "AI Application Layer",
        "Agentic Infrastructure",
        "Anthropic",
        "OpenAI",
        "LLM",
        "AI Agents",
    ):
        assert old_label not in rendered


def test_trends_count_only_last_seven_days_when_enough_digest_days_exist():
    documents = [
        ("2026-07-19", "Lạm phát CPI hạ nhiệt."),
        ("2026-07-18", "Bản tin không có chủ đề taxonomy."),
        ("2026-07-17", "Bản tin tổng hợp."),
        ("2026-07-12", "Inflation tăng mạnh nhưng đã quá cửa sổ 7 ngày."),
    ]

    summary = build_trends_summary(documents, as_of=date(2026, 7, 19))

    assert summary["window_days"] == 7
    assert _trend(summary, "inflation")["count"] == 1


def test_trend_direction_compares_recent_and_earlier_halves():
    documents = [
        ("2026-07-13", "Bản tin đầu kỳ."),
        ("2026-07-14", "Bản tin đầu kỳ."),
        ("2026-07-15", "Bản tin đầu kỳ."),
        ("2026-07-16", "Lạm phát được công bố."),
        ("2026-07-17", "CPI tiếp tục được theo dõi."),
        ("2026-07-18", "Bản tin cuối kỳ."),
        ("2026-07-19", "Bản tin cuối kỳ."),
    ]

    summary = build_trends_summary(documents, as_of=date(2026, 7, 19))

    assert _trend(summary, "inflation")["direction"] == "Tăng"


def test_trends_output_contains_no_more_than_five_topics():
    text = " ".join(
        (
            "Lạm phát CPI",
            "tỷ giá USD/VND",
            "tăng trưởng GDP",
            "tăng trưởng tín dụng",
            "nợ xấu NPL",
            "VN-Index",
            "giá vàng",
            "trái phiếu",
        )
    )
    summary = build_trends_summary([("2026-07-19", text)], as_of=date(2026, 7, 19))

    assert len(summary["trends"]) <= 5


def test_trends_use_fourteen_days_only_when_fewer_than_three_recent_digests():
    documents = [
        ("2026-07-19", "Bản tin mới."),
        ("2026-07-18", "Bản tin mới."),
        ("2026-07-10", "NPL và nợ xấu ngân hàng."),
    ]

    summary = build_trends_summary(documents, as_of=date(2026, 7, 19))

    assert summary["window_days"] == 14
    assert _trend(summary, "bad_debt")["count"] == 1


def test_write_trends_to_kv_graceful_degrade_when_missing_secrets(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_KV_NAMESPACE_ID", raising=False)

    result = write_trends_to_kv({})  # không raise là pass

    assert result is False


def test_kv_ttl_is_query_parameter_and_success_json_required(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        text = '{"success":true}'

        def json(self):
            return {"success": True}

    def fake_put(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr("deliver.requests.put", fake_put)
    assert _kv_put("key", "value", "account", "namespace", "token", ttl_seconds=123)
    assert calls[0]["params"] == {"expiration_ttl": 123}
    assert calls[0]["data"] == b"value"
    assert "files" not in calls[0]


def test_group_chat_ids_and_multi_chat_delivery(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "123,-1001234567890,123")
    assert get_telegram_chat_ids() == ["123", "-1001234567890"]

    calls = []

    def fake_send(token, chat_id, text, reply_markup=None):
        calls.append(chat_id)
        return chat_id != "123"

    monkeypatch.setattr("deliver.send_telegram_message", fake_send)
    assert deliver("digest hợp lệ", dry_run=False) is False
    assert calls == ["123", "-1001234567890"]

"""Tests cho RSS Việt Nam và cơ chế lỗi độc lập từng feed."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sources import vietnam_news  # noqa: E402


class Entry(dict):
    published_parsed = time.gmtime(datetime.now(timezone.utc).timestamp())


class FakeResponse:
    content = b"rss"

    def raise_for_status(self) -> None:
        return None


def test_required_official_rss_urls_are_configured():
    urls = {config["url"] for config in vietnam_news.FEEDS.values()}
    assert urls == {
        "https://tuoitre.vn/nld/rss/kinh-te.rss",
        "https://tuoitre.vn/nld/rss/nld/kinh-te/tai-chinh-chung-khoan.rss",
        "https://tuoitre.vn/nld/rss/quoc-te.rss",
    }


def test_rss_description_is_used_without_article_scrape(monkeypatch):
    entry = Entry(
        title="Ngân hàng công bố kết quả kinh doanh",
        link="https://tuoitre.vn/ngan-hang-ket-qua.htm",
        description="<p>Lợi nhuận tăng nhưng nợ xấu cần theo dõi.</p>",
    )
    monkeypatch.setattr(vietnam_news.requests, "get", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(
        vietnam_news.feedparser,
        "parse",
        lambda content: {"entries": [entry]},
    )

    items = vietnam_news._fetch_one_feed(
        "test",
        {
            "name": "Tuổi Trẻ test",
            "url": "https://tuoitre.vn/test.rss",
            "default_type": "macro",
        },
        hours=24,
    )

    assert len(items) == 1
    assert items[0].type == "banking"
    assert items[0].source == "tuoitre.vn"
    assert items[0].raw_text == "Lợi nhuận tăng nhưng nợ xấu cần theo dõi."


def test_one_feed_failure_does_not_drop_other_feeds(monkeypatch):
    configs = {
        "bad": {"name": "Bad", "url": "https://example.com/bad", "default_type": "macro"},
        "good": {"name": "Good", "url": "https://example.com/good", "default_type": "markets"},
    }
    expected = object()

    def fake_fetch(feed_key, feed_config, hours):
        if feed_key == "bad":
            raise RuntimeError("feed error")
        return [expected]

    monkeypatch.setattr(vietnam_news, "FEEDS", configs)
    monkeypatch.setattr(vietnam_news, "_fetch_one_feed", fake_fetch)

    assert vietnam_news.fetch() == [expected]

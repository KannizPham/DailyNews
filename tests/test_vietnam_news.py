"""Compatibility checks for the Vietnamese subset of the new registry."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sources import vietnam_news  # noqa: E402
from sources.economic_news import DEFAULT_FRESHNESS_HOURS  # noqa: E402


def test_vietnam_wrapper_contains_official_and_media_sources():
    assert len(vietnam_news.VIETNAM_SOURCES) == 9
    assert {source.key for source in vietnam_news.VIETNAM_SOURCES} >= {
        "sbv_news",
        "nso_releases",
        "baochinhphu_economy",
        "ssc_news",
        "hnx_rss",
        "vietnamplus_economy",
        "vnexpress_business",
        "tuoitre_economy",
        "tuoitre_finance",
    }


def test_default_daily_freshness_is_36_hours():
    assert DEFAULT_FRESHNESS_HOURS == 36
    assert all(
        source.freshness_hours == (72 if source.official else 36)
        for source in vietnam_news.VIETNAM_SOURCES
    )

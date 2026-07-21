"""Compatibility wrapper for the diversified economic source registry.

New code should import :mod:`sources.economic_news`.  This module remains so
older callers do not silently fall back to the former three-feed/24-hour
implementation.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional

from sources.economic_news import DEFAULT_FRESHNESS_HOURS, SOURCES, SourceConfig, fetch as fetch_registry

VIETNAM_SOURCES: tuple[SourceConfig, ...] = tuple(
    source
    for source in SOURCES
    if source.key
    in {
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
)

# Kept as a read-only-style mapping for integrations which previously
# inspected vietnam_news.FEEDS.
FEEDS = {
    source.key: {
        "name": source.name,
        "url": source.url,
        "default_type": source.default_type,
        "kind": source.kind,
    }
    for source in VIETNAM_SOURCES
}


def fetch(
    hours: int = DEFAULT_FRESHNESS_HOURS,
    *,
    seen_ids: Optional[set[str]] = None,
    session: Any = None,
) -> list:
    """Return Vietnamese sources using 36h, while official sources keep 72h."""
    configs = tuple(
        replace(source, freshness_hours=hours)
        if not source.official
        else source
        for source in VIETNAM_SOURCES
    )
    kwargs = {"configs": configs, "seen_ids": seen_ids or set()}
    if session is not None:
        kwargs["session"] = session
    return fetch_registry(**kwargs).items

"""Nguồn funding — gộp nhiều RSS feed (§4 của brief).

Ưu tiên SEA (e27, DealStreetAsia) vì đó là theater của operator, cộng thêm
TechCrunch (global, tier 1). Mỗi feed fail độc lập — 1 feed lỗi không làm
mất các feed khác (đúng tinh thần "1 nguồn lỗi không sập pipeline").
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import feedparser
import requests

from pipeline import Item, make_item_id

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 20

# Quyết định: brief §4 liệt kê TechCrunch, Crunchbase News, Sifted, e27, Tech
# in Asia, DealStreetAsia, KrASIA. Nhiệm vụ bước 2 chỉ yêu cầu rõ TechCrunch +
# e27 + DealStreetAsia — giữ đúng 3 feed đó để không lan man, domain còn lại
# (sifted.eu, techinasia.com, kr-asia.com) đã có sẵn trong SOURCE_TIERS (bước
# 3) để dễ bổ sung feed URL sau mà không cần sửa registry.
FEEDS: dict[str, str] = {
    "techcrunch.com": "https://techcrunch.com/category/venture/feed/",
    "e27.co": "https://e27.co/feed/",
    "dealstreetasia.com": "https://www.dealstreetasia.com/feed",
}


def _parse_entry_published(entry) -> Optional[datetime]:
    """feedparser chuẩn hoá published_parsed/updated_parsed thành time.struct_time
    (UTC-naive theo feedparser convention) khi parse được; fallback None nếu
    feed không có ngày hợp lệ."""
    parsed = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _fetch_one_feed(domain: str, feed_url: str, hours: int) -> list[Item]:
    items: list[Item] = []
    try:
        resp = requests.get(
            feed_url,
            timeout=DEFAULT_TIMEOUT_S,
            headers={"User-Agent": "Mozilla/5.0 (morning-intel-agent)"},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("funding.py: lỗi fetch %s, bỏ qua feed này: %s", domain, exc)
        return []

    try:
        parsed = feedparser.parse(resp.content)
    except Exception as exc:  # noqa: BLE001 - feed lỗi không sập nguồn khác
        logger.warning("funding.py: lỗi parse feed %s: %s", domain, exc)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    for entry in parsed.get("entries", []):
        try:
            title = (entry.get("title") or "").strip()
            url = (entry.get("link") or "").strip()
            if not title or not url:
                continue

            published_at = _parse_entry_published(entry) or datetime.now(
                timezone.utc
            )
            if published_at < cutoff:
                continue

            summary = (entry.get("summary") or entry.get("description") or "").strip()

            items.append(
                Item(
                    id=make_item_id(url, title),
                    type="funding",
                    title=title,
                    url=url,
                    source=domain,
                    published_at=published_at,
                    raw_text=summary,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 1 entry lỗi không sập feed
            logger.warning("funding.py: bỏ qua 1 entry lỗi parse (%s): %s", domain, exc)
            continue

    logger.info("funding.py: %s -> %d item trong %dh gần nhất", domain, len(items), hours)
    return items


def fetch(hours: int = 24) -> list[Item]:
    """Gộp mọi feed funding trong FEEDS. Mỗi feed fail độc lập."""
    all_items: list[Item] = []
    for domain, feed_url in FEEDS.items():
        try:
            all_items.extend(_fetch_one_feed(domain, feed_url, hours))
        except Exception as exc:  # noqa: BLE001 - an toàn kép, không để 1 feed sập cả hàm
            logger.warning("funding.py: lỗi không mong đợi với %s: %s", domain, exc)
            continue
    return all_items


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for it in fetch():
        print(f"- [{it.source}] {it.title} ({it.url})")

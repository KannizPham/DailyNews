"""Nguồn GitHub Trending — không có RSS chính chủ, dùng dịch vụ RSS không
chính thức (mshibanami/GitHubTrendingRSS, ổn định, free, không cần key).

Quyết định: brief §4 nói "RSS không chính thức hoặc parse trang trending" —
chọn RSS không chính thức để tránh tự viết HTML scraper (dễ vỡ khi GitHub đổi
markup). type="product" vì đây là tín hiệu "cái gì đang được build/dùng",
gần với mục `product` hơn `research`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import feedparser
import requests

from pipeline import Item, make_item_id

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 20
GITHUB_TRENDING_RSS = "https://mshibanami.github.io/GitHubTrendingRSS/daily/all.xml"


def fetch() -> list[Item]:
    """Lấy GitHub Trending (daily, all languages) qua RSS không chính thức.

    Feed này không có ngày publish đáng tin cậy theo từng repo (toàn bộ feed
    cập nhật 1 lần/ngày) -> dùng thời điểm fetch hiện tại làm published_at.
    """
    try:
        resp = requests.get(
            GITHUB_TRENDING_RSS,
            timeout=DEFAULT_TIMEOUT_S,
            headers={"User-Agent": "Mozilla/5.0 (morning-intel-agent)"},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("github_trending.py: lỗi fetch RSS, bỏ qua nguồn này: %s", exc)
        return []

    try:
        parsed = feedparser.parse(resp.content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("github_trending.py: lỗi parse feed: %s", exc)
        return []

    now = datetime.now(timezone.utc)
    items: list[Item] = []

    for entry in parsed.get("entries", []):
        try:
            title = (entry.get("title") or "").strip()
            url = (entry.get("link") or "").strip()
            if not title or not url:
                continue
            summary = (entry.get("summary") or "").strip()

            items.append(
                Item(
                    id=make_item_id(url, title),
                    type="product",
                    title=title,
                    url=url,
                    source="github.com",
                    published_at=now,
                    raw_text=summary,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("github_trending.py: bỏ qua 1 entry lỗi parse: %s", exc)
            continue

    logger.info("github_trending.py: lấy được %d repo trending", len(items))
    return items


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for it in fetch():
        print(f"- {it.title} ({it.url})")

"""Tin kinh tế Việt Nam và quốc tế từ các RSS công khai của Tuổi Trẻ.

Mỗi feed được tải và parse độc lập. Module chỉ dùng title, link, thời gian và
mô tả có sẵn trong RSS; không tải toàn bộ nội dung bài báo.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import feedparser
import requests

from pipeline import Item, make_item_id

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 20
SOURCE_DOMAIN = "tuoitre.vn"

# Dictionary có key ổn định để có thể thêm/xóa feed mà không đổi luồng fetch.
FEEDS: dict[str, dict[str, str]] = {
    "tuoitre_kinh_te": {
        "name": "Tuổi Trẻ - Kinh tế",
        "url": "https://tuoitre.vn/nld/rss/kinh-te.rss",
        "default_type": "macro",
    },
    "tuoitre_tai_chinh_chung_khoan": {
        "name": "Tuổi Trẻ - Tài chính chứng khoán",
        "url": "https://tuoitre.vn/nld/rss/nld/kinh-te/tai-chinh-chung-khoan.rss",
        "default_type": "markets",
    },
    "tuoitre_quoc_te": {
        "name": "Tuổi Trẻ - Quốc tế",
        "url": "https://tuoitre.vn/nld/rss/quoc-te.rss",
        "default_type": "geopolitics",
    },
}

_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "technology",
        (
            "trí tuệ nhân tạo",
            "artificial intelligence",
            "bán dẫn",
            "semiconductor",
            "chip",
            "trung tâm dữ liệu",
            "data center",
            "điện toán đám mây",
        ),
    ),
    (
        "banking",
        (
            "ngân hàng",
            "nợ xấu",
            "tiền gửi",
            "huy động vốn",
            "cho vay",
            "nim",
            "casa",
            "basel",
        ),
    ),
    (
        "markets",
        (
            "chứng khoán",
            "vn-index",
            "hnx-index",
            "cổ phiếu",
            "trái phiếu",
            "lợi suất",
            "s&p 500",
            "nasdaq",
            "dow jones",
            "giá vàng",
            "giá dầu",
            "thị trường vốn",
        ),
    ),
    (
        "corporate",
        (
            "doanh nghiệp",
            "công ty",
            "tập đoàn",
            "doanh thu",
            "lợi nhuận",
            "kết quả kinh doanh",
            "định giá",
            "cổ tức",
            "m&a",
            "mua bán sáp nhập",
        ),
    ),
    (
        "geopolitics",
        (
            "địa chính trị",
            "thuế quan",
            "trừng phạt",
            "chiến tranh thương mại",
            "xung đột",
            "biển đông",
            "nato",
            "wto",
            "kiểm soát xuất khẩu",
        ),
    ),
    (
        "macro",
        (
            "gdp",
            "cpi",
            "lạm phát",
            "pmi",
            "lãi suất",
            "tỷ giá",
            "tín dụng",
            "chính sách tiền tệ",
            "ngân sách",
            "đầu tư công",
            "thuế",
            "xuất khẩu",
            "nhập khẩu",
            "tăng trưởng",
        ),
    ),
)


def _clean_rss_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(without_tags).split())


def _parse_entry_published(entry: Any) -> Optional[datetime]:
    parsed = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _parse_tuoitre_url_published(url: str) -> Optional[datetime]:
    """Tuổi Trẻ thường nhúng YYYYMMDD vào ID cuối URL bài viết."""
    match = re.search(r"(?<!\d)(20\d{6})(?:\d{6,})?(?!\d)", url)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def classify_item(text: str, default_type: str) -> str:
    """Phân loại xác định bằng keyword; không gọi LLM ở bước lấy nguồn."""
    normalized = text.lower()
    for item_type, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return item_type
    return default_type


def _fetch_one_feed(
    feed_key: str,
    feed_config: dict[str, str],
    hours: int,
) -> list[Item]:
    feed_url = feed_config["url"]
    feed_name = feed_config["name"]
    default_type = feed_config["default_type"]

    try:
        response = requests.get(
            feed_url,
            timeout=DEFAULT_TIMEOUT_S,
            headers={"User-Agent": "Mozilla/5.0 (daily-news-agent)"},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(
            "vietnam_news.py: lỗi fetch %s (%s), bỏ qua feed: %s",
            feed_name,
            feed_url,
            exc,
        )
        return []

    try:
        parsed = feedparser.parse(response.content)
    except Exception as exc:  # noqa: BLE001 - một feed lỗi không sập nguồn khác
        logger.warning(
            "vietnam_news.py: lỗi parse %s, bỏ qua feed: %s",
            feed_name,
            exc,
        )
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items: list[Item] = []

    for entry in parsed.get("entries", []):
        try:
            title = _clean_rss_text(entry.get("title"))
            url = (entry.get("link") or "").strip()
            if not title or not url:
                continue

            # URL date đáng tin hơn pubDate khi báo đưa lại bài cũ lên RSS.
            # Nếu cả URL lẫn RSS đều không có ngày thì bỏ bài: giả định "now"
            # sẽ biến bài cũ thành bài mới và phá freshness của bản tin.
            published_at = _parse_tuoitre_url_published(url) or _parse_entry_published(entry)
            if published_at is None:
                logger.info(
                    "vietnam_news.py: bỏ entry không xác định được ngày trong %s: %s",
                    feed_name,
                    title,
                )
                continue
            if published_at < cutoff:
                continue

            # Ưu tiên mô tả RSS; không gọi sang trang bài viết để scrape.
            raw_text = _clean_rss_text(
                entry.get("summary") or entry.get("description") or ""
            )
            item_type = classify_item(
                f"{title} {raw_text}",
                default_type,
            )
            items.append(
                Item(
                    id=make_item_id(url, title),
                    type=item_type,
                    title=title,
                    url=url,
                    source=SOURCE_DOMAIN,
                    published_at=published_at,
                    raw_text=raw_text,
                )
            )
        except Exception as exc:  # noqa: BLE001 - một entry lỗi không sập feed
            logger.warning(
                "vietnam_news.py: bỏ qua entry lỗi trong %s: %s",
                feed_key,
                exc,
            )

    logger.info(
        "vietnam_news.py: %s -> %d item trong %dh gần nhất",
        feed_name,
        len(items),
        hours,
    )
    return items


def fetch(hours: int = 24) -> list[Item]:
    """Gộp các RSS; mỗi feed lỗi độc lập và chỉ trả các item còn hợp lệ."""
    all_items: list[Item] = []
    for feed_key, feed_config in FEEDS.items():
        try:
            all_items.extend(_fetch_one_feed(feed_key, feed_config, hours))
        except Exception as exc:  # noqa: BLE001 - lớp bảo vệ cuối cho từng feed
            logger.warning(
                "vietnam_news.py: lỗi không mong đợi ở feed %s: %s",
                feed_key,
                exc,
            )
    return all_items


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for item in fetch():
        print(f"- [{item.type}] {item.title} ({item.url})")

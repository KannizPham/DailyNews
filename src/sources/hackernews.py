"""Nguồn Hacker News qua Algolia HN API (free, không cần key).

Lọc points > 50 theo §4. Fail độc lập như arxiv.py.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

import requests

from pipeline import Item, make_item_id

logger = logging.getLogger(__name__)

HN_API_URL = "https://hn.algolia.com/api/v1/search_by_date"
MIN_POINTS = 50
DEFAULT_TIMEOUT_S = 20


def fetch(hours: int = 24, min_points: int = MIN_POINTS) -> list[Item]:
    """Gọi Algolia HN API, trả về list[Item] type="research" với points > min_points.

    Quyết định: dùng tag=story để loại comment/poll, và numericFilters theo
    created_at_i + points để Algolia tự lọc phía server (nhanh hơn lọc tay).
    """
    since_ts = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    params = {
        "tags": "story",
        "numericFilters": f"points>{min_points},created_at_i>{since_ts}",
        "hitsPerPage": 50,
    }

    try:
        resp = requests.get(HN_API_URL, params=params, timeout=DEFAULT_TIMEOUT_S)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("hackernews.py: lỗi gọi API, bỏ qua nguồn này: %s", exc)
        return []

    try:
        data = resp.json()
    except ValueError as exc:
        logger.warning("hackernews.py: lỗi parse JSON, bỏ qua nguồn này: %s", exc)
        return []

    items: list[Item] = []
    for hit in data.get("hits", []):
        try:
            title = hit.get("title") or ""
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            points = hit.get("points") or 0
            created_at_i = hit.get("created_at_i")

            if not title or created_at_i is None:
                continue

            # Dùng created_at_i (unix timestamp) thay vì parse string
            # "created_at" — Algolia trả format không cố định microsecond
            # (đôi khi có .%f, đôi khi không), timestamp là cách ổn định nhất.
            published_at = datetime.fromtimestamp(created_at_i, tz=timezone.utc)

            items.append(
                Item(
                    id=make_item_id(url, title),
                    type="research",
                    title=title,
                    url=url,
                    source="news.ycombinator.com",
                    published_at=published_at,
                    raw_text=f"HN points: {points}",
                    # Dùng points làm phần "đã chấm điểm sẵn từ nguồn" cộng
                    # vào heuristic Stage 1. Chuẩn hoá bằng log để HN points
                    # (có thể lên tới hàng nghìn) không áp đảo hoàn toàn điểm
                    # keyword/freshness (vốn chỉ trong khoảng 0-15). Ví dụ:
                    # points=50 -> ~2.0, points=500 -> ~3.1, points=1165 -> ~3.5.
                    score=math.log10(max(points, 1)) * 1.3,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hackernews.py: bỏ qua 1 hit lỗi parse: %s", exc)
            continue

    logger.info(
        "hackernews.py: lấy được %d item (points>%d) trong %dh gần nhất",
        len(items),
        min_points,
        hours,
    )
    return items


def fetch_show_hn(hours: int = 48, max_results: int = 30) -> list[Item]:
    """Nguồn riêng cho "Show HN" — launch post của dự án/sản phẩm mới.

    Quyết định: KHÔNG áp ngưỡng points như fetch() chính. Lý do: dự án thật sự
    sớm (kiểu Cursor đăng Show HN 8 lần trước khi nổi) thường có points THẤP
    lúc mới đăng — ngưỡng points>50 của fetch() sẽ loại bỏ chính loại tín hiệu
    này. type="product" để feed vào nhánh "LĂNG KÍNH NGƯỜI XÂY" của digest;
    knowledge base (companies trong kb.json) sẽ tự lộ ra pattern "đăng lại
    nhiều lần" qua count tăng dần theo ngày, không cần xử lý đặc biệt ở đây.
    """
    since_ts = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    params = {
        "tags": "show_hn",
        "numericFilters": f"created_at_i>{since_ts}",
        "hitsPerPage": max_results,
    }

    try:
        resp = requests.get(HN_API_URL, params=params, timeout=DEFAULT_TIMEOUT_S)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("hackernews.py (show_hn): lỗi gọi API, bỏ qua nguồn này: %s", exc)
        return []

    try:
        data = resp.json()
    except ValueError as exc:
        logger.warning("hackernews.py (show_hn): lỗi parse JSON, bỏ qua nguồn này: %s", exc)
        return []

    items: list[Item] = []
    for hit in data.get("hits", []):
        try:
            title = hit.get("title") or ""
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            points = hit.get("points") or 0
            created_at_i = hit.get("created_at_i")

            if not title or created_at_i is None:
                continue

            published_at = datetime.fromtimestamp(created_at_i, tz=timezone.utc)

            items.append(
                Item(
                    id=make_item_id(url, title),
                    type="product",
                    title=title,
                    url=url,
                    source="news.ycombinator.com",
                    published_at=published_at,
                    raw_text=f"Show HN | points: {points}",
                    # Points thường thấp ở launch post -> trọng số nhẹ, đừng
                    # để áp đảo (khác fetch() chính, vốn ưu tiên points cao).
                    score=math.log10(max(points, 1)) * 0.5,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hackernews.py (show_hn): bỏ qua 1 hit lỗi parse: %s", exc)
            continue

    logger.info(
        "hackernews.py (show_hn): lấy được %d item launch post trong %dh gần nhất",
        len(items),
        hours,
    )
    return items


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for it in fetch():
        print(f"- [{it.published_at}] ({it.score}) {it.title} ({it.url})")
    print("--- show_hn ---")
    for it in fetch_show_hn():
        print(f"- [{it.published_at}] ({it.score}) {it.title} ({it.url})")

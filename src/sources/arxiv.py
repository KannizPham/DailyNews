"""Nguồn arXiv API — categories cs.LG, cs.AI, stat.ML, q-fin, 24h gần nhất.

Fail độc lập: lỗi network/parse ở đây KHÔNG được làm sập main.py — trả về
list rỗng và log warning (xem §4: "một nguồn chết thì pipeline vẫn chạy tiếp").
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

from pipeline import Item, make_item_id

logger = logging.getLogger(__name__)

ARXIV_API_URL = "https://export.arxiv.org/api/query"
CATEGORIES = ["cs.LG", "cs.AI", "stat.ML", "q-fin"]
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
DEFAULT_TIMEOUT_S = 20


def fetch(hours: int = 24, max_results: int = 50) -> list[Item]:
    """Gọi arXiv API, trả về list[Item] type="research" trong `hours` gần nhất.

    Brief không nói rõ cách lọc 24h: arXiv API không hỗ trợ filter theo ngày
    trực tiếp trong query đơn giản, nên quyết định: lấy max_results mới nhất
    sort theo submittedDate desc, sau đó filter lại bằng code theo published_at.
    """
    category_query = " OR ".join(f"cat:{c}" for c in CATEGORIES)
    params = {
        "search_query": category_query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": max_results,
    }

    try:
        resp = requests.get(ARXIV_API_URL, params=params, timeout=DEFAULT_TIMEOUT_S)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("arxiv.py: lỗi gọi API, bỏ qua nguồn này: %s", exc)
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        logger.warning("arxiv.py: lỗi parse XML, bỏ qua nguồn này: %s", exc)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items: list[Item] = []

    for entry in root.findall("atom:entry", ATOM_NS):
        try:
            title_el = entry.find("atom:title", ATOM_NS)
            summary_el = entry.find("atom:summary", ATOM_NS)
            published_el = entry.find("atom:published", ATOM_NS)
            link = entry.find("atom:id", ATOM_NS)

            if title_el is None or published_el is None or link is None:
                continue

            published_at = datetime.strptime(
                published_el.text.strip(), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)

            if published_at < cutoff:
                # Kết quả đã sort theo ngày giảm dần -> có thể dừng sớm.
                break

            title = " ".join(title_el.text.split())
            summary = " ".join(summary_el.text.split()) if summary_el is not None else ""
            url = link.text.strip()

            items.append(
                Item(
                    id=make_item_id(url, title),
                    type="research",
                    title=title,
                    url=url,
                    source="arxiv.org",
                    published_at=published_at,
                    raw_text=summary,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 1 entry lỗi không được sập cả fetch
            logger.warning("arxiv.py: bỏ qua 1 entry lỗi parse: %s", exc)
            continue

    logger.info("arxiv.py: lấy được %d item trong %dh gần nhất", len(items), hours)
    return items


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for it in fetch():
        print(f"- [{it.published_at}] {it.title} ({it.url})")

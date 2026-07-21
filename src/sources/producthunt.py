"""Nguồn Product Hunt — API GraphQL chính thức (cần PRODUCTHUNT_TOKEN, free
nhưng phải đăng ký developer account).

Theo yêu cầu: nếu thiếu token thì KHÔNG block pipeline — log warning, trả về
[] và pipeline vẫn chạy tiếp với các nguồn khác. Khi operator add
PRODUCTHUNT_TOKEN, module này tự động active mà không cần sửa code.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests

from pipeline import Item, make_item_id

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 20
PRODUCTHUNT_GRAPHQL_URL = "https://api.producthunt.com/v2/api/graphql"

# Lấy top post hôm nay, sort theo votes.
QUERY = """
query TodayPosts {
  posts(order: VOTES, first: 15) {
    edges {
      node {
        id
        name
        tagline
        url
        votesCount
        createdAt
      }
    }
  }
}
"""


def fetch() -> list[Item]:
    """Gọi Product Hunt GraphQL API. Trả về [] nếu thiếu PRODUCTHUNT_TOKEN."""
    token = os.environ.get("PRODUCTHUNT_TOKEN")
    if not token:
        logger.warning(
            "producthunt.py: thiếu PRODUCTHUNT_TOKEN trong env — bỏ qua nguồn "
            "Product Hunt (không block pipeline). Lấy token tại "
            "https://api.producthunt.com/v2/oauth/applications"
        )
        return []

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            PRODUCTHUNT_GRAPHQL_URL,
            json={"query": QUERY},
            headers=headers,
            timeout=DEFAULT_TIMEOUT_S,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("producthunt.py: lỗi gọi API, bỏ qua nguồn này: %s", exc)
        return []

    try:
        data = resp.json()
    except ValueError as exc:
        logger.warning("producthunt.py: lỗi parse JSON, bỏ qua nguồn này: %s", exc)
        return []

    if "errors" in data:
        logger.warning("producthunt.py: API trả lỗi: %s", data["errors"])
        return []

    items: list[Item] = []
    edges = (data.get("data") or {}).get("posts", {}).get("edges", [])

    for edge in edges:
        try:
            node = edge.get("node", {})
            title = node.get("name") or ""
            tagline = node.get("tagline") or ""
            url = node.get("url") or ""
            votes = node.get("votesCount") or 0
            created_at_raw = node.get("createdAt")

            if not title or not url:
                continue

            published_at = datetime.now(timezone.utc)
            if created_at_raw:
                try:
                    published_at = datetime.fromisoformat(
                        created_at_raw.replace("Z", "+00:00")
                    )
                except ValueError:
                    pass

            items.append(
                Item(
                    id=make_item_id(url, title),
                    type="product",
                    title=title,
                    url=url,
                    source="producthunt.com",
                    published_at=published_at,
                    raw_text=f"{tagline} | votes: {votes}",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("producthunt.py: bỏ qua 1 item lỗi parse: %s", exc)
            continue

    logger.info("producthunt.py: lấy được %d item", len(items))
    return items


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for it in fetch():
        print(f"- {it.title} ({it.url})")

"""Nguồn deep tech / kỹ thuật (§4b của brief): IEEE Spectrum RSS + arXiv
eess/physics.app-ph/cond-mat. Mục tiêu KHÁC các nguồn khác: không lấy tin
"ai ra mắt gì", mà lấy nội dung có thể giải thích nguyên lý kỹ thuật.

Mọi item ở đây gắn type="deep_tech".
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from pipeline import Item, make_item_id

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 20

IEEE_SPECTRUM_RSS = "https://spectrum.ieee.org/feeds/feed.rss"

ARXIV_API_URL = "https://export.arxiv.org/api/query"
# eess: electrical engineering/systems science; physics.app-ph: applied
# physics; cond-mat: condensed matter (vật liệu, semiconductor, battery).
DEEP_TECH_CATEGORIES = ["eess.SY", "eess.SP", "physics.app-ph", "cond-mat.mtrl-sci"]
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _fetch_ieee_spectrum(hours: int) -> list[Item]:
    try:
        resp = requests.get(
            IEEE_SPECTRUM_RSS,
            timeout=DEFAULT_TIMEOUT_S,
            headers={"User-Agent": "Mozilla/5.0 (morning-intel-agent)"},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("deep_tech.py: lỗi fetch IEEE Spectrum, bỏ qua: %s", exc)
        return []

    try:
        parsed = feedparser.parse(resp.content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("deep_tech.py: lỗi parse IEEE Spectrum RSS: %s", exc)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items: list[Item] = []

    for entry in parsed.get("entries", []):
        try:
            title = (entry.get("title") or "").strip()
            url = (entry.get("link") or "").strip()
            if not title or not url:
                continue

            parsed_date = getattr(entry, "published_parsed", None) or getattr(
                entry, "updated_parsed", None
            )
            published_at = (
                datetime(*parsed_date[:6], tzinfo=timezone.utc)
                if parsed_date
                else datetime.now(timezone.utc)
            )
            if published_at < cutoff:
                continue

            summary = (entry.get("summary") or "").strip()

            items.append(
                Item(
                    id=make_item_id(url, title),
                    type="deep_tech",
                    title=title,
                    url=url,
                    source="spectrum.ieee.org",
                    published_at=published_at,
                    raw_text=summary,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("deep_tech.py: bỏ qua 1 entry IEEE Spectrum lỗi parse: %s", exc)
            continue

    logger.info("deep_tech.py: IEEE Spectrum -> %d item trong %dh gần nhất", len(items), hours)
    return items


def _fetch_arxiv_deep_tech(hours: int, max_results: int = 50) -> list[Item]:
    """Logic giống sources/arxiv.py nhưng category khác (eess/physics/cond-mat)
    và gắn type="deep_tech" thay vì "research" — tách riêng module theo §4b
    (đừng tái dùng arxiv.fetch() trực tiếp vì type khác nhau)."""
    category_query = " OR ".join(f"cat:{c}" for c in DEEP_TECH_CATEGORIES)
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
        logger.warning("deep_tech.py: lỗi gọi arXiv API, bỏ qua: %s", exc)
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        logger.warning("deep_tech.py: lỗi parse XML arXiv, bỏ qua: %s", exc)
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
                break

            title = " ".join(title_el.text.split())
            summary = " ".join(summary_el.text.split()) if summary_el is not None else ""
            url = link.text.strip()

            items.append(
                Item(
                    id=make_item_id(url, title),
                    type="deep_tech",
                    title=title,
                    url=url,
                    source="arxiv.org",
                    published_at=published_at,
                    raw_text=summary,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("deep_tech.py: bỏ qua 1 entry arXiv lỗi parse: %s", exc)
            continue

    logger.info(
        "deep_tech.py: arXiv (eess/physics/cond-mat) -> %d item trong %dh gần nhất",
        len(items),
        hours,
    )
    return items


def fetch(hours: int = 24) -> list[Item]:
    """Gộp IEEE Spectrum + arXiv deep tech categories. Mỗi nguồn fail độc lập."""
    items: list[Item] = []
    for fetch_fn, name in [
        (lambda: _fetch_ieee_spectrum(hours), "ieee_spectrum"),
        (lambda: _fetch_arxiv_deep_tech(hours), "arxiv_deep_tech"),
    ]:
        try:
            items.extend(fetch_fn())
        except Exception as exc:  # noqa: BLE001
            logger.warning("deep_tech.py: nguồn %s lỗi, bỏ qua: %s", name, exc)
    return items


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for it in fetch():
        print(f"- [{it.source}] {it.title} ({it.url})")

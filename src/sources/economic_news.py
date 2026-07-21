"""Diversified, fail-isolated economic news source registry.

The module intentionally does not guess that an undated entry is new.  It
normalizes URLs before constructing item IDs, rejects non-economic technology
stories, and returns source-level diagnostics for the workflow log.
"""

from __future__ import annotations

import html
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import feedparser
import requests

from pipeline import Item, make_item_id
from sources.source_registry import get_tier

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_FRESHNESS_HOURS = 36
OFFICIAL_FRESHNESS_HOURS = 72
MAX_STORIES_PER_DOMAIN = 2
USER_AGENT = "DailyNews economic source reader/1.0"


@dataclass(frozen=True)
class SourceConfig:
    key: str
    name: str
    url: str
    kind: str = "rss"
    default_type: str = "macro"
    official: bool = False
    freshness_hours: int = DEFAULT_FRESHNESS_HOURS
    fallback_url: Optional[str] = None


@dataclass
class SourceDiagnostic:
    source: str
    http_status: Optional[int] = None
    fetched: int = 0
    fresh: int = 0
    unseen: int = 0
    accepted: int = 0
    error: str = ""


@dataclass
class SourceBatch:
    items: list[Item] = field(default_factory=list)
    diagnostics: list[SourceDiagnostic] = field(default_factory=list)


# URLs are deliberately kept in one auditable registry.  Official listing
# pages use the same generic HTML parser; HNX discovers the actual XML links
# from its official RSS catalogue so a feed-path change needs no code change.
SOURCES: tuple[SourceConfig, ...] = (
    SourceConfig(
        "sbv_news",
        "Ngân hàng Nhà nước Việt Nam",
        "https://sbv.gov.vn/vi/web/sbv_portal/tin-tuc-su-kien",
        kind="html",
        default_type="banking",
        official=True,
        freshness_hours=OFFICIAL_FRESHNESS_HOURS,
    ),
    SourceConfig(
        "nso_releases",
        "Cục Thống kê (NSO/GSO)",
        "https://www.nso.gov.vn/bao-cao-tinh-hinh-kinh-te-xa-hoi-hang-thang/",
        kind="html",
        default_type="macro",
        official=True,
        freshness_hours=OFFICIAL_FRESHNESS_HOURS,
    ),
    SourceConfig(
        "baochinhphu_economy",
        "Báo Điện tử Chính phủ - Kinh tế",
        "https://baochinhphu.vn/kinh-te.htm",
        kind="html",
        default_type="macro",
        official=True,
        freshness_hours=OFFICIAL_FRESHNESS_HOURS,
    ),
    SourceConfig(
        "ssc_news",
        "Ủy ban Chứng khoán Nhà nước",
        "https://ssc.gov.vn/webcenter/portal/ubck",
        kind="html",
        default_type="markets",
        official=True,
        freshness_hours=OFFICIAL_FRESHNESS_HOURS,
    ),
    SourceConfig(
        "hnx_rss",
        "Sở Giao dịch Chứng khoán Hà Nội (HNX)",
        "https://hnx.vn/vi-vn/rss.html",
        kind="rss_catalog",
        default_type="markets",
        official=True,
        freshness_hours=OFFICIAL_FRESHNESS_HOURS,
        fallback_url="https://hnx.vn/vi-vn/m-tin-tuc-hnx.html",
    ),
    SourceConfig(
        "vietnamplus_economy",
        "VietnamPlus - Kinh tế",
        "https://www.vietnamplus.vn/rss/kinhte-311.rss",
        default_type="macro",
    ),
    SourceConfig(
        "vnexpress_business",
        "VnExpress - Kinh doanh",
        "https://vnexpress.net/rss/kinh-doanh.rss",
        default_type="corporate",
    ),
    SourceConfig(
        "tuoitre_economy",
        "Tuổi Trẻ - Kinh tế",
        "https://tuoitre.vn/nld/rss/kinh-te.rss",
        default_type="macro",
    ),
    SourceConfig(
        "tuoitre_finance",
        "Tuổi Trẻ - Tài chính chứng khoán",
        "https://tuoitre.vn/nld/rss/nld/kinh-te/tai-chinh-chung-khoan.rss",
        default_type="markets",
    ),
    SourceConfig(
        "federal_reserve",
        "Federal Reserve - Monetary Policy",
        "https://www.federalreserve.gov/feeds/press_monetary.xml",
        default_type="macro",
        official=True,
        freshness_hours=OFFICIAL_FRESHNESS_HOURS,
    ),
    SourceConfig(
        "imf_rss",
        "International Monetary Fund (IMF) - RSS catalog",
        "https://www.imf.org/en/social-hub",
        kind="rss_catalog",
        default_type="macro",
        official=True,
        freshness_hours=OFFICIAL_FRESHNESS_HOURS,
        fallback_url="https://www.imf.org/en/news",
    ),
    SourceConfig(
        "bls_latest",
        "U.S. Bureau of Labor Statistics",
        "https://www.bls.gov/feed/bls_latest.rss",
        default_type="macro",
        official=True,
        freshness_hours=OFFICIAL_FRESHNESS_HOURS,
    ),
    SourceConfig(
        "world_bank_search",
        "World Bank Documents & Reports",
        "https://search.worldbank.org/api/v3/wds?format=json&rows=20&os=0&fl=docdt,abstracts,docty&sort=docdt&order=desc",
        kind="world_bank_json",
        default_type="macro",
        official=True,
        freshness_hours=OFFICIAL_FRESHNESS_HOURS,
    ),
)


_CATEGORY_KEYWORDS: Mapping[str, tuple[str, ...]] = {
    "banking": ("ngân hàng", "bank", "tín dụng", "credit", "nợ xấu", "liquidity"),
    "markets": (
        "chứng khoán", "stock", "bond", "trái phiếu", "market", "cổ phiếu",
        "vn-index", "hnx", "gold", "vàng", "oil", "dầu", "yield",
    ),
    "corporate": (
        "doanh nghiệp", "company", "corporate", "doanh thu", "revenue",
        "lợi nhuận", "profit", "m&a", "merger", "acquisition", "cổ tức",
    ),
    "geopolitics": (
        "thuế quan", "tariff", "sanction", "trừng phạt", "trade war",
        "xung đột", "conflict", "supply chain", "chuỗi cung ứng",
    ),
    "macro": (
        "gdp", "cpi", "inflation", "lạm phát", "interest rate", "lãi suất",
        "tỷ giá", "exchange rate", "employment", "việc làm", "fiscal",
        "monetary", "tăng trưởng", "growth", "xuất khẩu", "nhập khẩu",
    ),
}
_TECH_TERMS = (
    "artificial intelligence", " ai ", "trí tuệ nhân tạo", "semiconductor",
    "bán dẫn", "chip", "data center", "trung tâm dữ liệu", "fintech",
    "ngân hàng số", "digital bank", "cloud computing",
)
_ECONOMIC_IMPACT_TERMS = (
    "đầu tư", "investment", "capex", "doanh thu", "revenue", "chi phí",
    "cost", "năng suất", "productivity", "việc làm", "employment", "jobs",
    "lợi nhuận", "profit", "thị phần", "market share", "chuỗi cung ứng",
    "supply chain", "xuất khẩu", "export", "thuế", "tax", "gdp",
)
_TRACKING_QUERY_PREFIXES = ("utm_",)
_TRACKING_QUERY_NAMES = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower() or "https"
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{parsed.port}" if parsed.port else ""
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_NAMES
        and not key.lower().startswith(_TRACKING_QUERY_PREFIXES)
    ]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((scheme, host + port, path, "", urlencode(sorted(query)), ""))


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _normalize_title(title: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", title.lower())
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    return {
        token
        for token in re.findall(r"[a-z0-9]+", plain)
        if len(token) > 2 and token not in {"the", "and", "cho", "cua", "voi", "from"}
    }


def titles_describe_same_event(left: str, right: str) -> bool:
    left_tokens, right_tokens = _normalize_title(left), _normalize_title(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = left_tokens & right_tokens
    union = left_tokens | right_tokens
    return len(overlap) >= 3 and len(overlap) / len(union) >= 0.55


def classify_item(text: str, default_type: str) -> Optional[str]:
    normalized = f" {text.lower()} "
    has_tech = any(term in normalized for term in _TECH_TERMS)
    if has_tech:
        if any(term in normalized for term in _ECONOMIC_IMPACT_TERMS):
            return "technology"
        # Pure launches/benchmarks must not sneak through under a generic
        # source's default category.
        return None
    for item_type, keywords in _CATEGORY_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            return item_type
    return default_type if default_type != "technology" else None


def _parse_datetime(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        parsed = None
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d",
            "%d/%m/%Y %H:%M", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y",
        ):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError):
                return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _entry_datetime(entry: Any) -> Optional[datetime]:
    structured = entry.get("published_parsed") or entry.get("updated_parsed")
    if structured:
        try:
            return datetime(*structured[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    return _parse_datetime(entry.get("published") or entry.get("updated"))


def _rss_records(content: bytes, base_url: str) -> list[dict[str, object]]:
    parsed = feedparser.parse(content)
    records: list[dict[str, object]] = []
    for entry in parsed.get("entries", []):
        title = _clean_text(entry.get("title"))
        link = urljoin(base_url, (entry.get("link") or "").strip())
        if title and link:
            records.append(
                {
                    "title": title,
                    "url": link,
                    "published_at": _entry_datetime(entry),
                    "raw_text": _clean_text(
                        entry.get("summary") or entry.get("description") or ""
                    ),
                }
            )
    return records


_ANCHOR_RE = re.compile(
    r"<a\b[^>]*?href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<title>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_DATE_RE = re.compile(
    r"(?P<date>(?:\d{1,2}/\d{1,2}/\d{4})(?:\s+\d{1,2}:\d{2})?|"
    r"(?:20\d{2}-\d{2}-\d{2})(?:T\d{2}:\d{2}:\d{2}Z?)?|"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2})",
    re.IGNORECASE,
)


def _html_records(content: bytes, base_url: str) -> list[dict[str, object]]:
    document = content.decode("utf-8", errors="replace")
    records: list[dict[str, object]] = []
    for match in _ANCHOR_RE.finditer(document):
        title = _clean_text(match.group("title"))
        href = urljoin(base_url, html.unescape(match.group("href")))
        if len(title) < 12 or href.startswith(("javascript:", "mailto:")):
            continue
        context = _clean_text(document[max(0, match.start() - 220) : match.end() + 220])
        date_match = _DATE_RE.search(context)
        published_at = _parse_datetime(date_match.group("date")) if date_match else None
        records.append(
            {"title": title, "url": href, "published_at": published_at, "raw_text": ""}
        )
    return records


def _world_bank_records(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    documents = payload.get("documents") or {}
    values = documents.values() if isinstance(documents, dict) else documents
    records: list[dict[str, object]] = []
    for document in values if isinstance(values, Iterable) else []:
        if not isinstance(document, dict):
            continue
        title = _clean_text(document.get("display_title") or document.get("docna"))
        url = str(document.get("pdfurl") or document.get("url") or "").strip()
        if title and url:
            records.append(
                {
                    "title": title,
                    "url": url,
                    "published_at": _parse_datetime(document.get("docdt")),
                    "raw_text": _clean_text(document.get("abstracts") or ""),
                }
            )
    return records


def _feed_urls_from_catalog(content: bytes, base_url: str) -> list[str]:
    document = content.decode("utf-8", errors="replace")
    urls: list[str] = []
    for match in _ANCHOR_RE.finditer(document):
        href = html.unescape(match.group("href"))
        lowered = href.lower()
        if "rss" in lowered or "feed" in lowered or lowered.endswith((".xml", ".rss")):
            candidate = urljoin(base_url, href)
            if candidate != base_url and candidate not in urls:
                urls.append(candidate)
    return urls[:8]


def _request(session: Any, url: str) -> Any:
    response = session.get(
        url,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, application/json, text/html"},
    )
    response.raise_for_status()
    return response


def _fetch_records(config: SourceConfig, session: Any) -> tuple[list[dict[str, object]], int]:
    response = _request(session, config.url)
    status = response.status_code
    if config.kind == "rss":
        return _rss_records(response.content, config.url), status
    if config.kind == "html":
        return _html_records(response.content, config.url), status
    if config.kind == "world_bank_json":
        return _world_bank_records(response.json()), status
    if config.kind != "rss_catalog":
        raise ValueError(f"Unsupported source kind: {config.kind}")

    records: list[dict[str, object]] = []
    feed_urls = _feed_urls_from_catalog(response.content, config.url)
    for feed_url in feed_urls:
        try:
            feed_response = _request(session, feed_url)
            records.extend(_rss_records(feed_response.content, feed_url))
        except requests.RequestException as exc:
            logger.warning("RSS con của %s lỗi (%s): %s", config.name, feed_url, exc)
    if records or not config.fallback_url:
        return records, status
    fallback = _request(session, config.fallback_url)
    return _html_records(fallback.content, config.fallback_url), fallback.status_code


def _records_to_candidates(
    config: SourceConfig,
    records: Sequence[dict[str, object]],
    diagnostic: SourceDiagnostic,
    seen_ids: set[str],
    now: datetime,
) -> list[Item]:
    candidates: list[Item] = []
    cutoff = now - timedelta(hours=config.freshness_hours)
    for record in records:
        published_at = record.get("published_at")
        if not isinstance(published_at, datetime):
            continue
        if published_at < cutoff or published_at > now + timedelta(hours=6):
            continue
        diagnostic.fresh += 1
        title = str(record["title"])
        original_url = str(record["url"])
        canonical_url = canonicalize_url(original_url)
        item_id = make_item_id(canonical_url, title)
        legacy_id = make_item_id(original_url, title)
        if item_id in seen_ids or legacy_id in seen_ids:
            continue
        diagnostic.unseen += 1
        raw_text = str(record.get("raw_text") or "")
        item_type = classify_item(f"{title} {raw_text}", config.default_type)
        if item_type is None:
            continue
        candidates.append(
            Item(
                id=item_id,
                type=item_type,
                title=title,
                url=canonical_url,
                source=_domain(canonical_url),
                published_at=published_at,
                raw_text=raw_text,
                source_tier=get_tier(canonical_url),
            )
        )
    return candidates


def _deduplicate_and_cap(
    sourced_candidates: Sequence[tuple[str, Item]],
    diagnostics: Mapping[str, SourceDiagnostic],
) -> list[Item]:
    accepted: list[Item] = []
    canonical_urls: set[str] = set()
    domain_counts: dict[str, int] = {}
    for source_key, item in sourced_candidates:
        canonical_url = canonicalize_url(item.url)
        domain = _domain(canonical_url)
        if canonical_url in canonical_urls:
            continue
        if any(titles_describe_same_event(item.title, existing.title) for existing in accepted):
            continue
        if domain_counts.get(domain, 0) >= MAX_STORIES_PER_DOMAIN:
            continue
        canonical_urls.add(canonical_url)
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        accepted.append(item)
        diagnostics[source_key].accepted += 1
    return accepted


def deduplicate_and_cap_items(items: Sequence[Item]) -> list[Item]:
    """Apply canonical/event dedupe and the two-story domain cap to any pool."""
    diagnostic = SourceDiagnostic("combined")
    return _deduplicate_and_cap(
        [("combined", item) for item in items],
        {"combined": diagnostic},
    )


def format_diagnostic_summary(diagnostics: Sequence[SourceDiagnostic]) -> str:
    lines = ["Source | fetched | fresh | unseen | accepted | error"]
    lines.extend(
        f"{row.source} | {row.fetched} | {row.fresh} | {row.unseen} | "
        f"{row.accepted} | {row.error or '-'}"
        for row in diagnostics
    )
    return "\n".join(lines)


def fetch(
    seen_ids: Optional[set[str]] = None,
    *,
    configs: Sequence[SourceConfig] = SOURCES,
    session: Any = requests,
    now: Optional[datetime] = None,
) -> SourceBatch:
    """Fetch all configured sources without allowing one failure to fan out."""
    seen_ids = seen_ids or set()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    diagnostics: list[SourceDiagnostic] = []
    by_key: dict[str, SourceDiagnostic] = {}
    classified_counts: dict[str, int] = {}
    sourced_candidates: list[tuple[str, Item]] = []

    for config in configs:
        diagnostic = SourceDiagnostic(source=config.name)
        diagnostics.append(diagnostic)
        by_key[config.key] = diagnostic
        try:
            records, status = _fetch_records(config, session)
            diagnostic.http_status = status
            diagnostic.fetched = len(records)
            candidates = _records_to_candidates(config, records, diagnostic, seen_ids, now)
            classified_counts[config.key] = len(candidates)
            for item in candidates:
                sourced_candidates.append((config.key, item))
        except Exception as exc:  # noqa: BLE001 - isolation is a source-layer contract
            status = getattr(getattr(exc, "response", None), "status_code", None)
            diagnostic.http_status = status
            diagnostic.error = f"HTTP {status}" if status else type(exc).__name__
            logger.warning("Nguồn %s lỗi độc lập: %s", config.name, diagnostic.error)

    items = _deduplicate_and_cap(sourced_candidates, by_key)
    for config in configs:
        diagnostic = by_key[config.key]
        if not diagnostic.error:
            if diagnostic.fetched == 0:
                diagnostic.error = "no entries"
            elif diagnostic.fresh == 0:
                diagnostic.error = "all old or undated"
            elif diagnostic.unseen == 0:
                diagnostic.error = "all already seen"
            elif classified_counts.get(config.key, 0) == 0:
                diagnostic.error = "rejected by classification"
            elif diagnostic.accepted == 0:
                diagnostic.error = "deduplicated or domain cap"
        logger.info(
            "Nguồn %s: http_status=%s parsed=%d fresh=%d unseen=%d accepted=%d error=%s",
            config.name,
            diagnostic.http_status,
            diagnostic.fetched,
            diagnostic.fresh,
            diagnostic.unseen,
            diagnostic.accepted,
            diagnostic.error or "-",
        )
    logger.info("Source diagnostic summary:\n%s", format_diagnostic_summary(diagnostics))
    return SourceBatch(items=items, diagnostics=diagnostics)

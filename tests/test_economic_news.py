from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pipeline import make_item_id  # noqa: E402
from sources import economic_news  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "sources"
NOW = datetime(2026, 7, 21, 0, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, *, content: bytes = b"", status_code: int = 200, payload=None):
        self.content = content
        self.status_code = status_code
        self._payload = payload
        self.response = self

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            exc = requests.HTTPError(f"HTTP {self.status_code}")
            exc.response = self
            raise exc

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def rss_config(key: str, url: str, *, official: bool = False, hours: int = 36):
    return economic_news.SourceConfig(
        key=key,
        name=key,
        url=url,
        default_type="macro",
        official=official,
        freshness_hours=hours,
    )


def test_exact_required_source_urls_are_registered():
    assert {source.url for source in economic_news.SOURCES} == {
        "https://sbv.gov.vn/vi/web/sbv_portal/tin-tuc-su-kien",
        "https://www.nso.gov.vn/bao-cao-tinh-hinh-kinh-te-xa-hoi-hang-thang/",
        "https://baochinhphu.vn/kinh-te.htm",
        "https://ssc.gov.vn/webcenter/portal/ubck",
        "https://hnx.vn/vi-vn/rss.html",
        "https://www.vietnamplus.vn/rss/kinhte-311.rss",
        "https://vnexpress.net/rss/kinh-doanh.rss",
        "https://tuoitre.vn/nld/rss/kinh-te.rss",
        "https://tuoitre.vn/nld/rss/nld/kinh-te/tai-chinh-chung-khoan.rss",
        "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "https://www.imf.org/en/social-hub",
        "https://www.bls.gov/feed/bls_latest.rss",
        "https://search.worldbank.org/api/v3/wds?format=json&rows=20&os=0&fl=docdt,abstracts,docty&sort=docdt&order=desc",
    }


def test_feed_failure_is_isolated_and_diagnostics_explain_rejections():
    good_url, bad_url = "https://fixture.test/good.xml", "https://fixture.test/bad.xml"
    session = FakeSession(
        {
            good_url: FakeResponse(content=fixture_bytes("economic_feed.xml")),
            bad_url: FakeResponse(status_code=503),
        }
    )
    batch = economic_news.fetch(
        configs=(rss_config("good", good_url), rss_config("bad", bad_url)),
        session=session,
        now=NOW,
    )

    assert [item.type for item in batch.items] == ["banking", "technology"]
    good, bad = batch.diagnostics
    assert (good.http_status, good.fetched, good.fresh, good.unseen, good.accepted) == (
        200,
        5,
        3,
        3,
        2,
    )
    assert bad.http_status == 503
    assert bad.error == "HTTP 503"


def test_missing_timestamp_is_not_assumed_current():
    url = "https://fixture.test/feed.xml"
    batch = economic_news.fetch(
        configs=(rss_config("fixture", url),),
        session=FakeSession({url: FakeResponse(content=fixture_bytes("economic_feed.xml"))}),
        now=NOW,
    )
    assert all("no-date" not in item.url for item in batch.items)
    assert batch.diagnostics[0].fetched == 5
    assert batch.diagnostics[0].fresh == 3


def test_seen_count_is_distinct_from_fresh_count():
    url = "https://fixture.test/feed.xml"
    canonical = "https://example.com/economy/rates"
    title = "Ngân hàng trung ương điều chỉnh lãi suất chính sách"
    batch = economic_news.fetch(
        seen_ids={make_item_id(canonical, title)},
        configs=(rss_config("fixture", url),),
        session=FakeSession({url: FakeResponse(content=fixture_bytes("economic_feed.xml"))}),
        now=NOW,
    )
    diagnostic = batch.diagnostics[0]
    assert diagnostic.fresh == 3
    assert diagnostic.unseen == 2
    assert all(item.url != canonical for item in batch.items)


def test_official_release_uses_72_hours_while_media_uses_36():
    media_url, official_url = "https://fixture.test/media.xml", "https://fixture.test/official.xml"
    content = fixture_bytes("single_release.xml")
    batch = economic_news.fetch(
        configs=(
            rss_config("media", media_url, hours=36),
            rss_config("official", official_url, official=True, hours=72),
        ),
        session=FakeSession(
            {media_url: FakeResponse(content=content), official_url: FakeResponse(content=content)}
        ),
        now=NOW,
    )
    assert batch.diagnostics[0].fresh == 0
    assert batch.diagnostics[1].fresh == 1
    assert len(batch.items) == 1


def test_canonical_url_title_similarity_and_domain_cap():
    def item(number: int, title: str, url: str):
        return economic_news.Item(
            id=str(number), type="macro", title=title, url=url,
            source=economic_news._domain(url), published_at=NOW,
        )

    diagnostics = {
        "a": economic_news.SourceDiagnostic("a"),
        "b": economic_news.SourceDiagnostic("b"),
    }
    accepted = economic_news._deduplicate_and_cap(
        [
            ("a", item(1, "Fed giữ nguyên lãi suất chính sách tháng Bảy", "https://example.com/a?utm_source=x")),
            ("b", item(2, "Fed giữ nguyên lãi suất chính sách trong tháng Bảy", "https://other.test/b")),
            ("a", item(3, "Báo cáo GDP quý mới", "https://example.com/c")),
            ("a", item(4, "Thị trường trái phiếu tăng thanh khoản", "https://example.com/d")),
        ],
        diagnostics,
    )
    assert [entry.url for entry in accepted] == ["https://example.com/a?utm_source=x", "https://example.com/c"]
    assert diagnostics["a"].accepted == 2
    assert diagnostics["b"].accepted == 0


def test_world_bank_json_is_parsed_without_network():
    url = "https://fixture.test/world-bank"
    payload = json.loads((FIXTURE_DIR / "world_bank.json").read_text(encoding="utf-8"))
    config = economic_news.SourceConfig(
        "worldbank", "World Bank", url, kind="world_bank_json",
        official=True, freshness_hours=72,
    )
    batch = economic_news.fetch(
        configs=(config,), session=FakeSession({url: FakeResponse(payload=payload)}), now=NOW
    )
    assert len(batch.items) == 1
    assert batch.items[0].url == "https://documents.worldbank.org/report/growth"
    assert batch.items[0].type == "macro"


def test_rss_catalog_discovers_child_feeds_and_isolates_child_failure():
    catalog = "https://catalog.test/rss.html"
    good_feed = "https://catalog.test/feeds/official.xml"
    bad_feed = "https://catalog.test/feeds/broken.xml"
    config = economic_news.SourceConfig(
        "catalog", "Official catalog", catalog, kind="rss_catalog",
        official=True, freshness_hours=72,
    )
    session = FakeSession(
        {
            catalog: FakeResponse(content=fixture_bytes("rss_catalog.html")),
            good_feed: FakeResponse(content=fixture_bytes("single_release.xml")),
            bad_feed: FakeResponse(status_code=503),
        }
    )
    batch = economic_news.fetch(configs=(config,), session=session, now=NOW)
    assert len(batch.items) == 1
    assert batch.diagnostics[0].http_status == 200
    assert [call[0] for call in session.calls] == [catalog, good_feed, bad_feed]


def test_diagnostics_distinguish_empty_old_seen_and_classification_rejection():
    empty_url = "https://fixture.test/empty.xml"
    old_url = "https://fixture.test/old.xml"
    seen_url = "https://fixture.test/seen.xml"
    tech_url = "https://fixture.test/tech.xml"
    title = "Công bố chỉ số giá tiêu dùng và lạm phát tháng mới"
    canonical = "https://official.example.gov/releases/cpi"
    session = FakeSession(
        {
            empty_url: FakeResponse(content=b"<rss><channel></channel></rss>"),
            old_url: FakeResponse(content=fixture_bytes("single_release.xml")),
            seen_url: FakeResponse(content=fixture_bytes("single_release.xml")),
            tech_url: FakeResponse(content=fixture_bytes("pure_technology.xml")),
        }
    )
    batch = economic_news.fetch(
        seen_ids={make_item_id(canonical, title)},
        configs=(
            rss_config("empty", empty_url),
            rss_config("old", old_url, hours=36),
            rss_config("seen", seen_url, official=True, hours=72),
            rss_config("tech", tech_url),
        ),
        session=session,
        now=NOW,
    )
    assert [row.error for row in batch.diagnostics] == [
        "no entries",
        "all old or undated",
        "all already seen",
        "rejected by classification",
    ]


def test_diagnostic_table_has_required_workflow_columns():
    table = economic_news.format_diagnostic_summary(
        [economic_news.SourceDiagnostic("Test", 200, 4, 3, 2, 1, "")]
    )
    assert table.splitlines()[0] == "Source | fetched | fresh | unseen | accepted | error"
    assert table.splitlines()[1] == "Test | 4 | 3 | 2 | 1 | -"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Ra mắt chatbot AI và benchmark model", None),
        ("Đầu tư AI giúp giảm chi phí và tăng năng suất", "technology"),
    ],
)
def test_technology_requires_explicit_economic_impact(text, expected):
    assert economic_news.classify_item(text, "macro") == expected

"""Unit test cho source_registry.py — domain lạ phải default tier 3."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sources.source_registry import get_display_name, get_domain, get_tier  # noqa: E402


def test_known_tier1_domain():
    assert get_tier("sbv.gov.vn") == 1
    assert get_tier("https://www.federalreserve.gov/feeds/press_monetary.xml") == 1
    assert get_tier("https://search.worldbank.org/api/v3/wds") == 1


def test_known_tier2_domain():
    assert get_tier("vietnamplus.vn") == 2
    assert get_tier("www.vnexpress.net") == 2
    assert get_tier("https://tuoitre.vn/kinh-te.htm") == 2


def test_known_tier3_domain():
    assert get_tier("news.ycombinator.com") == 3


def test_unknown_domain_defaults_tier3():
    assert get_tier("some-random-blog-nobody-heard-of.example.com") == 3


def test_get_domain_strips_scheme_and_www():
    assert get_domain("https://www.example.com/path") == "example.com"
    assert get_domain("example.com") == "example.com"


def test_subdomain_of_known_tier1_inherits_tier():
    assert get_tier("search.worldbank.org") == 1


def test_vietnamese_source_has_readable_display_name():
    assert get_display_name("https://tuoitre.vn/kinh-te.htm") == "Tuổi Trẻ"


if __name__ == "__main__":
    test_fns = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in test_fns:
        try:
            fn()
            print(f"PASS: {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL: {fn.__name__}: {exc}")
    print(f"\n{len(test_fns) - failed}/{len(test_fns)} passed")
    if failed:
        sys.exit(1)

"""Unit test cho memory.py — merge kb_update, dedupe seen_ids, save archive."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from memory import (  # noqa: E402
    load_kb,
    merge_kb_update,
    save_archive,
    save_kb,
    update_seen_ids,
)
import memory


def test_merge_kb_update_increments_count():
    kb = {"themes": {}, "companies": {}, "investors": {}, "tech": {}, "deep_tech": {}}
    update = {"themes": ["diffusion models"], "tech": ["optimal transport"]}

    kb = merge_kb_update(kb, update, today="2026-06-19")
    kb = merge_kb_update(kb, update, today="2026-06-20")

    assert kb["themes"]["diffusion models"]["count"] == 2
    assert kb["themes"]["diffusion models"]["first_seen"] == "2026-06-19"
    assert kb["themes"]["diffusion models"]["last_seen"] == "2026-06-20"


def test_merge_kb_update_handles_none_gracefully():
    kb = {"themes": {"x": {"count": 1}}, "companies": {}, "investors": {}, "tech": {}, "deep_tech": {}}
    result = merge_kb_update(kb, None)
    assert result["themes"]["x"]["count"] == 1  # không đổi gì


def test_merge_companies_updates_investors_too():
    kb = {"themes": {}, "companies": {}, "investors": {}, "tech": {}, "deep_tech": {}}
    update = {
        "companies": [
            {"name": "Acme", "round": "Series A", "investors": ["Sequoia"]}
        ]
    }
    kb = merge_kb_update(kb, update, today="2026-06-19")
    assert kb["companies"]["Acme"]["count"] == 1
    assert kb["investors"]["Sequoia"]["count"] == 1


def test_merge_deep_tech():
    kb = {"themes": {}, "companies": {}, "investors": {}, "tech": {}, "deep_tech": {}}
    update = {"deep_tech": [{"name": "solid-state battery", "domain": "battery"}]}
    kb = merge_kb_update(kb, update, today="2026-06-19")
    assert kb["deep_tech"]["solid-state battery"]["count"] == 1
    assert kb["deep_tech"]["solid-state battery"]["domain"] == "battery"


def test_save_and_load_kb_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "kb.json"
        kb = {"themes": {"a": {"count": 1}}, "companies": {}, "investors": {}, "tech": {}, "deep_tech": {}}
        save_kb(kb, path)
        loaded = load_kb(path)
        assert loaded["themes"]["a"]["count"] == 1


def test_load_kb_missing_file_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "does_not_exist.json"
        kb = load_kb(path)
        assert kb == {"themes": {}, "companies": {}, "investors": {}, "tech": {}, "deep_tech": {}}


def test_save_archive_creates_file():
    with tempfile.TemporaryDirectory() as tmp:
        archive_dir = Path(tmp) / "archive"
        path = save_archive("hello digest", archive_dir, date_str="2026-06-19")
        assert path.exists()
        assert path.read_text(encoding="utf-8").strip() == "hello digest"


def test_update_seen_ids_dedupes():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "seen.json"
        update_seen_ids(path, ["id1", "id2"])
        result = update_seen_ids(path, ["id2", "id3"])
        assert result == {"id1", "id2", "id3"}


def test_seen_json_order_is_stable():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "seen.json"
        update_seen_ids(path, ["id2", "id1", "id2"])
        first = path.read_text(encoding="utf-8")
        update_seen_ids(path, ["id1"])
        assert path.read_text(encoding="utf-8") == first
        assert json.loads(first)["seen_ids"] == ["id2", "id1"]


def test_archive_uses_vietnam_date(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 7, 20, 17, 30, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value

    monkeypatch.setattr(memory, "datetime", FixedDateTime)
    with tempfile.TemporaryDirectory() as tmp:
        path = save_archive("digest hợp lệ", Path(tmp))
        assert path.name == "2026-07-21.md"


def test_invalid_digest_cannot_overwrite_good_archive():
    with tempfile.TemporaryDirectory() as tmp:
        archive_dir = Path(tmp)
        path = save_archive("digest tốt", archive_dir, date_str="2026-07-20")
        with pytest.raises(ValueError):
            save_archive("Hiện tại chưa có dữ liệu mới được cập nhật trong hệ thống.", archive_dir, date_str="2026-07-20")
        assert path.read_text(encoding="utf-8").strip() == "digest tốt"


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

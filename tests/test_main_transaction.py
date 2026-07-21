from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import main as daily_main  # noqa: E402
from pipeline import AnalysisResult, Item  # noqa: E402


def make_item() -> Item:
    return Item(
        id="item-1",
        type="macro",
        title="Tin kinh tế hợp lệ",
        url="https://example.com/item-1",
        source="example.com",
        published_at=datetime.now(timezone.utc),
        raw_text="Nội dung nguồn.",
    )


def configure_common(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, item: Item) -> dict[str, int]:
    calls = {"items_kv": 0, "trends": 0, "notices": 0}
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr(daily_main, "THESIS_PATH", tmp_path / "thesis.yaml")
    monkeypatch.setattr(daily_main, "SEEN_PATH", tmp_path / "state" / "seen.json")
    monkeypatch.setattr(daily_main, "KB_PATH", tmp_path / "knowledge" / "kb.json")
    monkeypatch.setattr(daily_main, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(daily_main, "build_llm_client_or_none", lambda: object())
    monkeypatch.setattr(daily_main, "fetch_all_sources", lambda: [item])
    monkeypatch.setattr(daily_main, "filter_stage1", lambda *args, **kwargs: [item])
    monkeypatch.setattr(daily_main, "llm_rank_and_select", lambda *args, **kwargs: [item])
    verified = SimpleNamespace(
        item=item,
        source_tier=1,
        cross_confirmed=True,
        confidence_emoji="🟢",
    )
    monkeypatch.setattr(daily_main, "verify_stage", lambda *args, **kwargs: [verified])
    monkeypatch.setattr(daily_main, "enrich_items", lambda values: (values, None))
    monkeypatch.setattr(
        daily_main,
        "write_items_to_kv",
        lambda values: calls.__setitem__("items_kv", calls["items_kv"] + 1) or True,
    )
    monkeypatch.setattr(
        daily_main,
        "write_trends_to_kv",
        lambda **kwargs: calls.__setitem__("trends", calls["trends"] + 1) or True,
    )
    monkeypatch.setattr(
        daily_main,
        "notify_operator",
        lambda *args, **kwargs: calls.__setitem__("notices", calls["notices"] + 1) or True,
    )
    monkeypatch.setattr(daily_main, "build_inline_keyboard", lambda values: {"inline_keyboard": []})
    return calls


def assert_no_persistent_state(tmp_path: Path) -> None:
    assert not (tmp_path / "archive").exists()
    assert not (tmp_path / "knowledge" / "kb.json").exists()
    assert not (tmp_path / "state" / "seen.json").exists()


def test_top_items_empty_does_not_call_llm_or_mutate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    item = make_item()
    calls = configure_common(monkeypatch, tmp_path, item)
    monkeypatch.setattr(daily_main, "filter_stage1", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        daily_main,
        "analyze_stage2",
        lambda *args, **kwargs: pytest.fail("Stage 2 must not run"),
    )

    daily_main.main()

    assert calls == {"items_kv": 0, "trends": 0, "notices": 1}
    assert_no_persistent_state(tmp_path)


def test_selected_items_empty_does_not_mutate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    item = make_item()
    calls = configure_common(monkeypatch, tmp_path, item)
    monkeypatch.setattr(daily_main, "llm_rank_and_select", lambda *args, **kwargs: [])

    daily_main.main()

    assert calls == {"items_kv": 0, "trends": 0, "notices": 1}
    assert_no_persistent_state(tmp_path)


def test_stage2_failure_does_not_mutate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    item = make_item()
    calls = configure_common(monkeypatch, tmp_path, item)
    monkeypatch.setattr(
        daily_main,
        "analyze_stage2",
        lambda *args, **kwargs: AnalysisResult("", success=False, error_code="provider_unavailable"),
    )

    daily_main.main()

    assert calls == {"items_kv": 0, "trends": 0, "notices": 1}
    assert_no_persistent_state(tmp_path)


def test_delivery_failure_does_not_update_seen_or_other_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    item = make_item()
    calls = configure_common(monkeypatch, tmp_path, item)
    monkeypatch.setattr(
        daily_main,
        "analyze_stage2",
        lambda *args, **kwargs: AnalysisResult("**Bản tin hợp lệ**", success=True),
    )
    monkeypatch.setattr(daily_main, "deliver", lambda *args, **kwargs: False)

    daily_main.main()

    assert calls["items_kv"] == 1
    assert calls["trends"] == 0
    assert_no_persistent_state(tmp_path)


def test_success_updates_archive_kb_seen_and_kv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    item = make_item()
    calls = configure_common(monkeypatch, tmp_path, item)
    monkeypatch.setattr(
        daily_main,
        "analyze_stage2",
        lambda *args, **kwargs: AnalysisResult(
            "**KINH TẾ VĨ MÔ**\n* 🟢 Bản tin hợp lệ ([Nguồn](https://example.com))",
            success=True,
            kb_update={"themes": ["Lạm phát"]},
            engine="gemini-test-flash",
        ),
    )
    monkeypatch.setattr(daily_main, "deliver", lambda *args, **kwargs: True)

    daily_main.main()

    archives = list((tmp_path / "archive").glob("*.md"))
    assert len(archives) == 1
    assert "Bản tin hợp lệ" in archives[0].read_text(encoding="utf-8")
    kb = json.loads((tmp_path / "knowledge" / "kb.json").read_text(encoding="utf-8"))
    seen = json.loads((tmp_path / "state" / "seen.json").read_text(encoding="utf-8"))
    assert "Lạm phát" in kb["themes"]
    assert seen["seen_ids"] == ["item-1"]
    assert calls["items_kv"] == 1
    assert calls["trends"] == 1

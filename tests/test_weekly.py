from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import weekly  # noqa: E402


def write_daily(archive_dir: Path, name: str, text: str) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / name).write_text(text, encoding="utf-8")


def test_invalid_daily_archives_are_skipped(tmp_path: Path) -> None:
    write_daily(tmp_path, "2026-07-18.md", "**Bản tin hợp lệ**")
    write_daily(tmp_path, "2026-07-19.md", "[Stage 2 LỖI] provider error")
    write_daily(tmp_path, "2026-07-20.md", "Hiện tại chưa có dữ liệu mới được cập nhật.")

    result = weekly.load_recent_daily_archives(tmp_path)

    assert result == [("2026-07-18", "**Bản tin hợp lệ**")]


def test_missing_key_does_not_write_weekly_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    write_daily(archive_dir, "2026-07-18.md", "**Bản tin hợp lệ**")
    monkeypatch.setattr(weekly, "ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(weekly, "KB_PATH", tmp_path / "knowledge" / "kb.json")
    monkeypatch.setattr(weekly, "LLMClient", lambda: SimpleNamespace(has_api_key=False))
    monkeypatch.setattr(weekly, "notify_operator", lambda *args, **kwargs: True)

    weekly.main()

    assert not list(archive_dir.glob("weekly-*.md"))


def test_llm_failure_does_not_write_weekly_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    write_daily(archive_dir, "2026-07-18.md", "**Bản tin hợp lệ**")

    class FailingClient:
        has_api_key = True
        gemini_api_key = "secret"

        def generate(self, *args, **kwargs):
            raise RuntimeError("provider down")

    monkeypatch.setattr(weekly, "ARCHIVE_DIR", archive_dir)
    monkeypatch.setattr(weekly, "KB_PATH", tmp_path / "knowledge" / "kb.json")
    monkeypatch.setattr(weekly, "LLMClient", FailingClient)
    monkeypatch.setattr(weekly, "notify_operator", lambda *args, **kwargs: True)

    weekly.main()

    assert not list(archive_dir.glob("weekly-*.md"))

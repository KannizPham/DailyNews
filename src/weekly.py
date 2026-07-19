"""Entrypoint tổng hợp tuần (§9 brief, chạy Chủ nhật qua weekly.yml).

Đọc 7 file archive/ gần nhất + kb.json, gọi LLM với WEEKLY_SYSTEM_PROMPT, lưu
archive/weekly-YYYY-WW.md, gửi Telegram (tôn trọng DRY_RUN giống main.py).

Chạy local:
    DRY_RUN=true python src/weekly.py
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from deliver import deliver
from llm_client import LLMClient
from memory import load_kb
from pipeline import build_kb_summary
from prompts import WEEKLY_SYSTEM_PROMPT

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("weekly")

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = REPO_ROOT / "archive"
KB_PATH = REPO_ROOT / "knowledge" / "kb.json"

DAILY_ARCHIVE_COUNT = 7


def load_recent_daily_archives(archive_dir: Path = ARCHIVE_DIR, n: int = DAILY_ARCHIVE_COUNT) -> list[tuple[str, str]]:
    """Đọc n file archive/YYYY-MM-DD.md gần nhất nhất (loại weekly-*.md).

    Trả list (date_str, content) sắp theo ngày tăng dần.
    """
    if not archive_dir.exists():
        logger.warning("weekly.py: %s chưa tồn tại, không có archive để đọc.", archive_dir)
        return []

    daily_files = [
        p for p in archive_dir.glob("*.md") if not p.name.startswith("weekly-")
    ]
    # Tên file dạng YYYY-MM-DD.md sort lexicographic == sort theo ngày.
    daily_files.sort(key=lambda p: p.stem)
    recent = daily_files[-n:]

    results = []
    for p in recent:
        try:
            content = p.read_text(encoding="utf-8")
            results.append((p.stem, content))
        except OSError as exc:
            logger.warning("weekly.py: lỗi đọc %s, bỏ qua: %s", p, exc)
            continue
    return results


def build_weekly_prompt(daily_archives: list[tuple[str, str]], kb_summary: str) -> str:
    lines = [
        "=== KNOWLEDGE BASE TÍCH LUỸ (pattern, count) ===",
        kb_summary,
        "",
        f"=== {len(daily_archives)} DIGEST GẦN NHẤT ===",
    ]
    for date_str, content in daily_archives:
        lines.append(f"\n--- Digest {date_str} ---\n{content}")
    return "\n".join(lines)


def _week_str(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def main() -> None:
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    if dry_run:
        logger.info("DRY_RUN=true — chạy weekly, không gửi Telegram.")

    daily_archives = load_recent_daily_archives()
    if not daily_archives:
        msg = (
            "[weekly.py KHÔNG chạy được] Không có file archive/YYYY-MM-DD.md nào "
            "để tổng hợp. Chạy main.py vài ngày trước rồi chạy lại weekly.py."
        )
        logger.warning(msg)
        deliver(msg, dry_run=dry_run)
        return

    kb = load_kb(KB_PATH)
    kb_summary = build_kb_summary(kb)
    prompt = build_weekly_prompt(daily_archives, kb_summary)

    client = LLMClient()

if not client._model_order():
    msg = (
        "[weekly.py KHÔNG chạy được] Thiếu GEMINI_API_KEY "
        "trong biến môi trường — không thể gọi Gemini để tổng hợp tuần. "
        f"Đã đọc được {len(daily_archives)} digest archive + kb.json thành công."
    )
    logger.warning(msg)
    deliver(msg, dry_run=dry_run)
    return

    try:
        response = client.generate(prompt, system=WEEKLY_SYSTEM_PROMPT)
    except Exception as exc:  # noqa: BLE001 - lỗi LLM không được crash weekly.py
        msg = f"[weekly.py LỖI] Gọi LLM thất bại: {exc}"
        logger.error(msg)
        deliver(msg, dry_run=dry_run)
        return

    logger.info(
        "weekly.py Stage analyze [%s]: input_tokens=%s output_tokens=%s (log để "
        "kiểm chứng chi phí ~$0)",
        response.engine,
        response.input_tokens,
        response.output_tokens,
    )

    weekly_text = response.text.strip()

    now = datetime.now()
    week_str = _week_str(now)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    weekly_path = ARCHIVE_DIR / f"weekly-{week_str}.md"
    weekly_path.write_text(weekly_text + "\n", encoding="utf-8")
    logger.info("weekly.py: đã lưu tổng hợp tuần vào %s", weekly_path)

    deliver(weekly_text, dry_run=dry_run, now=now)


if __name__ == "__main__":
    main()

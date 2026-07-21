"""Entrypoint tổng hợp tuần (§9 brief, chạy Chủ nhật qua weekly.yml).

Đọc 7 file archive/ gần nhất + kb.json, gọi LLM với WEEKLY_SYSTEM_PROMPT, lưu
archive/weekly-YYYY-WW.md, gửi Telegram (tôn trọng DRY_RUN giống main.py).

Chạy local:
    DRY_RUN=true python src/weekly.py
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from deliver import deliver, notify_operator
from llm_client import LLMClient, sanitize_sensitive_text
from memory import atomic_write_text, is_valid_digest_text, load_kb
from pipeline import build_kb_summary
from prompts import WEEKLY_SYSTEM_PROMPT

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("weekly")
VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

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

    daily_files = [p for p in archive_dir.glob("*.md") if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", p.name)]
    # Tên file dạng YYYY-MM-DD.md sort lexicographic == sort theo ngày.
    daily_files.sort(key=lambda p: p.stem)
    results = []
    for p in reversed(daily_files):
        try:
            content = p.read_text(encoding="utf-8")
            if not is_valid_digest_text(content):
                logger.warning("weekly.py: bỏ archive không hợp lệ %s.", p)
                continue
            results.append((p.stem, content))
            if len(results) >= n:
                break
        except OSError as exc:
            logger.warning("weekly.py: lỗi đọc %s, bỏ qua: %s", p, exc)
            continue
    results.reverse()
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

    daily_archives = load_recent_daily_archives(ARCHIVE_DIR)
    if not daily_archives:
        msg = (
            "[weekly.py KHÔNG chạy được] Không có file archive/YYYY-MM-DD.md nào "
            "để tổng hợp. Chạy main.py vài ngày trước rồi chạy lại weekly.py."
        )
        logger.warning(msg)
        notify_operator(msg, dry_run=dry_run)
        return

    kb = load_kb(KB_PATH)
    kb_summary = build_kb_summary(kb)
    prompt = build_weekly_prompt(daily_archives, kb_summary)

    client = LLMClient()

    if not client.has_api_key:
        msg = (
            "Không tạo được bản tin tuần vì thiếu GEMINI_API_KEY. "
            "Không có weekly archive nào được ghi."
        )
        logger.warning(msg)
        notify_operator(msg, dry_run=dry_run)
        return

    try:
        response = client.generate(prompt, system=WEEKLY_SYSTEM_PROMPT)
    except Exception as exc:  # noqa: BLE001 - lỗi LLM không được crash weekly.py
        safe_error = sanitize_sensitive_text(exc, client.gemini_api_key)
        logger.error("weekly.py: Gemini thất bại: %s", safe_error)
        notify_operator(
            "Không tạo được bản tin tuần do Gemini tạm thời không khả dụng. "
            "Weekly archive chưa bị thay đổi.",
            dry_run=dry_run,
        )
        return

    logger.info(
        "weekly.py Stage analyze [%s]: input_tokens=%s output_tokens=%s (log để "
        "kiểm chứng chi phí ~$0)",
        response.engine,
        response.input_tokens,
        response.output_tokens,
    )

    weekly_text = response.text.strip()
    if not is_valid_digest_text(weekly_text):
        logger.error("weekly.py: response rỗng/không hợp lệ; từ chối delivery và archive.")
        notify_operator("Không tạo được bản tin tuần vì nội dung trả về không hợp lệ.", dry_run=dry_run)
        return

    now = datetime.now(VIETNAM_TZ)
    week_str = _week_str(now)
    weekly_path = ARCHIVE_DIR / f"weekly-{week_str}.md"
    if not deliver(weekly_text, dry_run=dry_run, now=now):
        logger.error("weekly.py: delivery thất bại; không ghi weekly archive.")
        return
    atomic_write_text(weekly_path, weekly_text + "\n")
    logger.info("weekly.py: đã gửi và lưu tổng hợp tuần vào %s", weekly_path)


if __name__ == "__main__":
    main()

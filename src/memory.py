"""Stage 3 — Accumulate (§6 của brief): đọc/ghi/append knowledge/kb.json,
lưu digest vào archive/YYYY-MM-DD.md, cập nhật state/seen.json.

Đây là LỚP TÍCH LUỸ quan trọng nhất theo brief — không cắt gọn.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

INVALID_DIGEST_MARKERS = (
    "[stage 2 lỗi]",
    "[stage 2 không chạy được]",
    "[weekly.py lỗi]",
    "tất cả gemini model",
    "raw provider error",
    "hiện tại chưa có dữ liệu mới được cập nhật",
    "không có tin mới kể từ lần chạy gần nhất",
)

EMPTY_KB = {
    "themes": {},
    "companies": {},
    "investors": {},
    "tech": {},
    "deep_tech": {},
}


def load_kb(path: Path) -> dict:
    if not path.exists():
        logger.info("memory.py: %s chưa tồn tại, khởi tạo kb rỗng.", path)
        return json.loads(json.dumps(EMPTY_KB))  # deep copy đơn giản

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key in EMPTY_KB:
            data.setdefault(key, {})
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("memory.py: lỗi đọc %s, dùng kb rỗng: %s", path, exc)
        return json.loads(json.dumps(EMPTY_KB))


def save_kb(kb: dict, path: Path) -> None:
    text = json.dumps(kb, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, text)


def _today_str() -> str:
    return datetime.now(VIETNAM_TZ).strftime("%Y-%m-%d")


def atomic_write_text(path: Path, text: str) -> None:
    """Ghi cùng filesystem rồi ``os.replace`` để không để lại file dở dang."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def is_valid_digest_text(digest_text: object) -> bool:
    """Chặn digest rỗng/thông báo vận hành/lỗi kỹ thuật khỏi persistent state."""
    if not isinstance(digest_text, str) or not digest_text.strip():
        return False
    normalized = " ".join(digest_text.lower().split())
    if any(marker in normalized for marker in INVALID_DIGEST_MARKERS):
        return False
    return not re.search(r"\bhttp\s*[45]\d{2}\b|gemini api (?:lỗi|error)", normalized)


def _merge_simple_list(kb_section: dict, names: list, today: str) -> None:
    """themes/tech: list[str] -> merge count + first_seen/last_seen."""
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        entry = kb_section.setdefault(
            name, {"count": 0, "first_seen": today, "last_seen": today, "notes": []}
        )
        entry["count"] = entry.get("count", 0) + 1
        entry["last_seen"] = today
        entry.setdefault("first_seen", today)
        entry.setdefault("notes", [])


def _merge_companies(kb_section: dict, investors_section: dict, companies: list, today: str) -> None:
    """Merge companies vào kb_section; investors nhắc tới trong mỗi round cũng
    được tích vào investors_section (đồng bộ, truyền trực tiếp thay vì global)."""
    for c in companies:
        if not isinstance(c, dict):
            continue
        name = (c.get("name") or "").strip()
        if not name:
            continue
        entry = kb_section.setdefault(
            name, {"count": 0, "rounds": [], "first_seen": today, "last_seen": today}
        )
        entry["count"] = entry.get("count", 0) + 1
        entry["last_seen"] = today
        entry.setdefault("first_seen", today)
        round_info = {"round": c.get("round"), "investors": c.get("investors", []), "date": today}
        entry.setdefault("rounds", []).append(round_info)

        # Investors xuất hiện trong company round cũng được tích vào "investors".
        for investor in c.get("investors") or []:
            if not isinstance(investor, str) or not investor.strip():
                continue
            inv_entry = investors_section.setdefault(
                investor.strip(),
                {"count": 0, "pattern_notes": [], "first_seen": today, "last_seen": today},
            )
            inv_entry["count"] = inv_entry.get("count", 0) + 1
            inv_entry["last_seen"] = today
            inv_entry.setdefault("first_seen", today)


def _merge_deep_tech(kb_section: dict, deep_tech_items: list, today: str) -> None:
    for dt in deep_tech_items:
        if not isinstance(dt, dict):
            continue
        name = (dt.get("name") or "").strip()
        if not name:
            continue
        entry = kb_section.setdefault(
            name,
            {
                "count": 0,
                "domain": dt.get("domain", ""),
                "first_seen": today,
                "last_seen": today,
            },
        )
        entry["count"] = entry.get("count", 0) + 1
        entry["last_seen"] = today
        entry.setdefault("first_seen", today)
        if dt.get("domain"):
            entry["domain"] = dt["domain"]


def merge_kb_update(kb: dict, kb_update: Optional[dict], today: Optional[str] = None) -> dict:
    """Merge block JSON từ Stage 2 vào kb.json đang có (§6 schema).

    kb_update có thể None (Stage 2 LLM lỗi/thiếu key/parse fail) -> không
    merge gì, trả kb nguyên trạng (KHÔNG crash).
    """
    today = today or _today_str()
    for key in EMPTY_KB:
        kb.setdefault(key, {})

    if not kb_update:
        logger.info("memory.py: không có kb_update (Stage 2 không trả JSON hợp lệ) -> giữ kb nguyên trạng.")
        return kb

    try:
        _merge_simple_list(kb["themes"], kb_update.get("themes", []) or [], today)
        _merge_simple_list(kb["tech"], kb_update.get("tech", []) or [], today)
        _merge_companies(
            kb["companies"], kb["investors"], kb_update.get("companies", []) or [], today
        )
        _merge_deep_tech(kb["deep_tech"], kb_update.get("deep_tech", []) or [], today)
    except Exception as exc:  # noqa: BLE001 - merge lỗi không được làm mất kb cũ
        logger.warning("memory.py: lỗi merge kb_update, giữ kb trước đó: %s", exc)

    return kb


def save_archive(digest_text: str, archive_dir: Path, date_str: Optional[str] = None) -> Path:
    """Lưu atomic digest hợp lệ theo ngày Việt Nam vào archive/YYYY-MM-DD.md."""
    if not is_valid_digest_text(digest_text):
        raise ValueError("Từ chối lưu archive vì digest rỗng hoặc là thông báo lỗi/vận hành.")
    date_str = date_str or _today_str()
    path = archive_dir / f"{date_str}.md"
    atomic_write_text(path, digest_text.rstrip() + "\n")
    logger.info("memory.py: đã lưu digest vào %s", path)
    return path


def load_seen_id_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw_ids = data.get("seen_ids", [])
        if not isinstance(raw_ids, list):
            return []
        # Dedupe nhưng giữ nguyên thứ tự lần đầu xuất hiện.
        return list(dict.fromkeys(value for value in raw_ids if isinstance(value, str) and value))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("memory.py: không đọc được %s, coi như rỗng: %s", path, exc)
        return []


def load_seen_ids(path: Path) -> set[str]:
    return set(load_seen_id_list(path))


def update_seen_ids(
    path: Path, new_ids: list[str], max_keep: int = 5000
) -> set[str]:
    """Thêm new_ids vào state/seen.json. Giới hạn max_keep để file không phình
    vô hạn theo thời gian (giữ id mới nhất — quyết định kỹ thuật vì brief
    không nói rõ chiến lược trim)."""
    existing_list = load_seen_id_list(path)
    existing = set(existing_list)
    merged = list(existing_list)
    for item_id in new_ids:
        if item_id and item_id not in existing:
            merged.append(item_id)
            existing.add(item_id)
    if len(merged) > max_keep:
        merged = merged[-max_keep:]

    text = json.dumps({"seen_ids": merged}, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, text)

    return set(merged)

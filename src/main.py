"""Entrypoint pipeline ngày — full pipeline theo §5/§12 brief.

Stage 0 (fetch tất cả nguồn) -> Stage 1 (dedupe + heuristic + LLM batch
ranking nếu có key, fallback heuristic thuần nếu không) -> Stage 1.5 (verify
tự động, gắn nhãn 🟢🟡🔴) -> Stage 2 (LLM analyze 6 mục) -> Stage 3
(accumulate vào kb.json + archive) -> Stage 4 (deliver Telegram).

Chạy local:
    DRY_RUN=true python src/main.py
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import yaml

from deliver import build_inline_keyboard, deliver, write_items_to_kv, write_trends_to_kv
from enrich import enrich_items
from llm_client import LLMClient
from memory import load_kb, merge_kb_update, save_archive, save_kb, update_seen_ids
from pipeline import (
    ThesisConfig,
    analyze_stage2,
    build_kb_summary,
    filter_stage1,
    llm_rank_and_select,
    tag_outside_candidates,
)
from prompts import ANALYSIS_SYSTEM_PROMPT
from sources import arxiv, deep_tech, funding, github_trending, hackernews, producthunt
from verify import verify_stage

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("main")

REPO_ROOT = Path(__file__).resolve().parent.parent
THESIS_PATH = REPO_ROOT / "config" / "thesis.yaml"
SEEN_PATH = REPO_ROOT / "state" / "seen.json"
KB_PATH = REPO_ROOT / "knowledge" / "kb.json"
ARCHIVE_DIR = REPO_ROOT / "archive"

# Stage 1 cắt còn ~40 item (tăng từ 20): TARGET_ALLOCATION giờ cần tới 11
# item/6 type, pool candidate cho LLM ranking phải rộng hơn để có lựa chọn
# thật, không chỉ đủ khít số lượng allocation.
STAGE1_KEEP_TOP_N = 40


def load_thesis(path: Path = THESIS_PATH) -> ThesisConfig:
    if not path.exists():
        logger.warning("Không tìm thấy %s, dùng ThesisConfig rỗng.", path)
        return ThesisConfig()

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return ThesisConfig(
        tracking=raw.get("tracking", []) or [],
        deep_tech_tracking=raw.get("deep_tech_tracking", []) or [],
        keywords_boost=raw.get("keywords_boost", []) or [],
        outside_lane_domains=raw.get("outside_lane_domains", []) or [],
    )


def load_seen_ids(path: Path = SEEN_PATH) -> set[str]:
    if not path.exists():
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("seen_ids", []))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Không đọc được %s, coi như chưa có gì đã gửi: %s", path, exc)
        return set()


SOURCE_FETCHERS = [
    (arxiv.fetch, "arxiv"),
    (hackernews.fetch, "hackernews"),
    (hackernews.fetch_show_hn, "hackernews_show_hn"),
    (funding.fetch, "funding"),
    (github_trending.fetch, "github_trending"),
    (producthunt.fetch, "producthunt"),
    (deep_tech.fetch, "deep_tech"),
]


def fetch_all_sources() -> list:
    """Stage 0 — gọi mọi nguồn, gộp lại. Mỗi nguồn fail độc lập (1 nguồn lỗi
    không sập pipeline, log warning, tiếp tục với nguồn còn lại)."""
    items = []
    for fetch_fn, name in SOURCE_FETCHERS:
        try:
            fetched = fetch_fn()
            items.extend(fetched)
            logger.info("Stage 0 — nguồn %s: %d item.", name, len(fetched))
        except Exception as exc:  # noqa: BLE001 - 1 nguồn lỗi không sập pipeline
            logger.warning("Nguồn %s lỗi, bỏ qua: %s", name, exc)
    return items


def build_llm_client_or_none() -> LLMClient | None:
    """Trả LLMClient nếu có ít nhất 1 key (Gemini/DeepSeek), ngược lại None.

    Quyết định: trả None thay vì instantiate client rồi luôn fail, để mọi nơi
    gọi LLM trong main.py kiểm tra được rõ ràng "có key hay không" và degrade
    gracefully, đúng yêu cầu không block khi thiếu key.
    """
    client = LLMClient()
    if not client._engine_order():
        logger.warning(
            "Không có GEMINI_API_KEY hoặc DEEPSEEK_API_KEY trong env -> mọi "
            "bước cần LLM (Stage 1 batch ranking, Stage 2 analyze) sẽ fallback/"
            "degrade gracefully, KHÔNG gọi LLM thật."
        )
        return None
    return client


def main() -> None:
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    if dry_run:
        logger.info("DRY_RUN=true — chạy full pipeline, không gửi Telegram thật.")

    thesis = load_thesis()
    seen_ids = load_seen_ids()
    llm_client = build_llm_client_or_none()

    logger.info("Stage 0 — fetch tất cả nguồn...")
    raw_items = fetch_all_sources()
    logger.info("Stage 0 — tổng %d item thô từ mọi nguồn.", len(raw_items))

    logger.info("Stage 1 — dedupe + heuristic score + cắt top %d...", STAGE1_KEEP_TOP_N)
    top_items = filter_stage1(
        raw_items, thesis, seen_ids=seen_ids, keep_top_n=STAGE1_KEEP_TOP_N
    )
    logger.info("Stage 1 — còn lại %d item sau filter heuristic.", len(top_items))

    # Không có nguồn nào tự emit type="outside" -> retag item match
    # outside_lane_domains (xem pipeline.tag_outside_candidates để biết lý do).
    top_items = tag_outside_candidates(top_items, thesis)

    logger.info("Stage 1 — LLM batch ranking + chọn theo cơ cấu (2 research, "
                "1 funding, 1 product, 1 deep_tech, 1 outside)...")
    selected_items = llm_rank_and_select(top_items, thesis, llm_client)
    logger.info("Stage 1 — đã chọn %d item cho Stage 1.5/2.", len(selected_items))

    logger.info("Stage 1.5 — verify tự động (tier + cross-reference + confidence)...")
    verified_items = verify_stage(selected_items, raw_items)
    for v in verified_items:
        logger.info(
            "  [%s] tier=%d cross_confirmed=%s -> %s %s",
            v.item.type, v.source_tier, v.cross_confirmed, v.confidence_emoji, v.item.title[:80],
        )

    logger.info(
        "Stage 1.6 — enrich: fetch nội dung thật cho item title-only (vd "
        "Hacker News) để Stage 2 không phải đoán từ tiêu đề trống..."
    )
    verified_items, og_image = enrich_items(verified_items)

    logger.info("Stage 2 — analyze (LLM thật nếu có key)...")
    kb = load_kb(KB_PATH)
    kb_summary = build_kb_summary(kb)
    analysis = analyze_stage2(
        verified_items, thesis, kb_summary, llm_client, ANALYSIS_SYSTEM_PROMPT
    )

    print("=" * 70)
    print("DIGEST HÔM NAY")
    print("=" * 70)
    print(analysis.digest_text)

    logger.info("Stage 3 — accumulate vào kb.json + archive + seen.json...")
    kb = merge_kb_update(kb, analysis.kb_update)
    save_kb(kb, KB_PATH)
    save_archive(analysis.digest_text, ARCHIVE_DIR)
    update_seen_ids(SEEN_PATH, [v.item.id for v in verified_items])
    logger.info("Stage 3 — xong: kb.json cập nhật, archive lưu, seen.json cập nhật.")

    logger.info(
        "Stage 3.5 — ghi context item + tóm tắt xu hướng (kb.json) lên Cloudflare "
        "KV cho tương tác real-time (Worker 'Hỏi sâu thêm'/'/refresh'/'/trends'); "
        "optional, bỏ qua nếu thiếu secret..."
    )
    kv_write_ok = write_items_to_kv(verified_items)
    write_trends_to_kv(kb)
    # Chỉ gắn nút "Hỏi sâu thêm" khi ghi KV thành công — nếu không, nút sẽ
    # bấm vào nhưng Worker không tìm thấy context (đã xảy ra thật khi thiếu
    # secret Cloudflare ở GitHub Actions, gây lỗi "Không tìm thấy context").
    inline_keyboard = build_inline_keyboard(verified_items) if kv_write_ok else None
    if not kv_write_ok:
        logger.warning(
            "Stage 3.5 — KV write thất bại/bỏ qua -> KHÔNG gắn nút 'Hỏi sâu "
            "thêm' để tránh nút chết (xem secret CLOUDFLARE_* nếu cần fix)."
        )

    logger.info("Stage 4 — deliver (Telegram nếu không DRY_RUN)...")
    delivered_ok = deliver(
        analysis.digest_text,
        dry_run=dry_run,
        reply_markup=inline_keyboard,
        photo_url=og_image,
    )
    logger.info("Stage 4 — kết quả gửi: %s", "THÀNH CÔNG" if delivered_ok else "THẤT BẠI (xem warning ở trên)")

    if analysis.json_parse_error:
        logger.warning(
            "Lưu ý: Stage 2 có vấn đề (%s) — digest đã gửi/in nhưng kb.json "
            "có thể không được cập nhật từ block JSON hôm nay.",
            analysis.json_parse_error,
        )


if __name__ == "__main__":
    main()

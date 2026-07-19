"""Stage 1.6 — Enrich: tải nội dung thật của trang gốc cho item đã chọn.

Vấn đề phát hiện: nhiều nguồn (đặc biệt hackernews.py/show_hn) chỉ có title,
KHÔNG có nội dung (raw_text="HN points: X") -> LLM ở Stage 2 phải "đoán" phân
tích từ một câu tiêu đề trống, gây ra chất lượng/độ sâu không đồng nhất giữa
các lần chạy (operator báo "chất lượng mỗi lúc một khác").

Chỉ fetch cho ~11 item ĐÃ ĐƯỢC CHỌN ở Stage 1 (không phải toàn bộ ~40-200 item
thô) — giữ số lượng HTTP call nhỏ, vẫn fail độc lập per-item (1 link chết
không sập cả bước enrich).

Tiện ích phụ: trong lúc fetch HTML, lấy luôn `<meta property="og:image">` của
item đầu tiên fetch thành công để dùng làm ảnh thumbnail gửi kèm Telegram
(xem deliver.py).
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 8
# Chỉ enrich item có raw_text quá ngắn (title-only) — arXiv/funding RSS
# thường đã có abstract/description đủ dùng, không cần fetch thêm để tránh
# tốn HTTP call không cần thiết.
ENRICH_THRESHOLD_CHARS = 300
MAX_ENRICHED_TEXT_CHARS = 2000
USER_AGENT = (
    "Mozilla/5.0 (compatible; MorningIntelBot/1.0; "
    "+https://github.com/KannizPham/DailyNews)"
)


class _TextAndOgImageExtractor(HTMLParser):
    """Parser HTML tối giản (stdlib, không thêm dependency): lấy text hiển thị
    (bỏ script/style/nav) + meta og:image. Không hoàn hảo nhưng đủ dùng để có
    nội dung thật thay vì không có gì."""

    SKIP_TAGS = {"script", "style", "nav", "footer", "header", "noscript"}

    def __init__(self) -> None:
        super().__init__()
        self.og_image: Optional[str] = None
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "meta" and attrs_dict.get("property") == "og:image":
            content = attrs_dict.get("content")
            if content and not self.og_image:
                self.og_image = content
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def get_text(self) -> str:
        return " ".join(self._chunks)


def fetch_article_content(url: str, timeout: int = DEFAULT_TIMEOUT_S) -> tuple[str, Optional[str]]:
    """Tải url, trả về (text rút gọn, og_image url hoặc None).

    Fail độc lập: bất kỳ lỗi gì (timeout, 403, parse lỗi...) trả ("", None),
    log warning, KHÔNG raise — gọi nơi khác không cần try/except riêng.
    """
    try:
        resp = requests.get(
            url, timeout=timeout, headers={"User-Agent": USER_AGENT}
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("enrich.py: fetch %s lỗi, bỏ qua: %s", url, exc)
        return "", None

    try:
        parser = _TextAndOgImageExtractor()
        parser.feed(resp.text)
    except Exception as exc:  # noqa: BLE001 - HTML lỗi không được sập pipeline
        logger.warning("enrich.py: parse HTML %s lỗi, bỏ qua: %s", url, exc)
        return "", None

    text = re.sub(r"\s+", " ", parser.get_text()).strip()
    return text[:MAX_ENRICHED_TEXT_CHARS], parser.og_image


def enrich_items(verified_items: list) -> tuple[list, Optional[str]]:
    """Với mỗi VerifiedItem có raw_text quá ngắn (title-only), fetch nội dung
    thật từ item.url và NỐI THÊM vào raw_text (giữ raw_text gốc, không xoá).

    Trả về (verified_items đã enrich tại chỗ, og_image đầu tiên fetch được
    hoặc None) — og_image dùng làm thumbnail Telegram (xem deliver.py).
    """
    first_og_image: Optional[str] = None

    for v in verified_items:
        it = v.item
        if len(it.raw_text) >= ENRICH_THRESHOLD_CHARS:
            continue

        text, og_image = fetch_article_content(it.url)
        if text:
            it.raw_text = f"{it.raw_text}\nNội dung trang gốc: {text}"
            logger.info(
                "enrich.py: đã fetch %d ký tự nội dung thật cho '%s'",
                len(text), it.title[:60],
            )
        if og_image and not first_og_image:
            first_og_image = og_image

    return verified_items, first_og_image

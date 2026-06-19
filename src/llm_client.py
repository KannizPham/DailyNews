"""Lớp trừu tượng gọi LLM: engine chính = Gemini Flash-Lite, fallback = DeepSeek
V4 Flash (endpoint Anthropic-compatible). Đổi engine bằng config/env, không
hardcode. Dùng raw HTTP (requests) thay vì SDK để giảm dependency — xem
requirements.txt.

Bước 1: chỉ cần interface sẵn sàng (generate()). main.py bước 1 CHƯA gọi LLM
thật (Stage 1 dùng heuristic rule-based thuần). Việc gọi LLM rẻ để xếp hạng
batch sẽ nối vào pipeline ở bước sau, dùng lại class này.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger(__name__)

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
# Endpoint Anthropic-compatible theo brief §1.
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/anthropic/v1/messages"

DEFAULT_TIMEOUT_S = 30
# Temperature thấp (mặc định API thường ~1.0) để output ổn định/nhất quán
# giữa các lần gọi — đây vốn là phân tích/báo cáo, không cần sáng tạo cao,
# và operator báo "chất lượng mỗi lần gửi khác nhau" -> giảm variance.
DEFAULT_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.3"))


class LLMError(RuntimeError):
    """Lỗi khi cả engine chính và fallback đều thất bại."""


@dataclass
class LLMResponse:
    text: str
    engine: str  # "gemini" | "deepseek"
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class LLMClient:
    """Gọi Gemini Flash-Lite trước; nếu lỗi/429/thiếu key thì fallback DeepSeek.

    Engine có thể override qua biến môi trường LLM_ENGINE=gemini|deepseek để
    test hoặc ép dùng 1 engine cụ thể.
    """

    def __init__(
        self,
        gemini_api_key: Optional[str] = None,
        deepseek_api_key: Optional[str] = None,
        forced_engine: Optional[str] = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.gemini_api_key = gemini_api_key or os.environ.get("GEMINI_API_KEY")
        self.deepseek_api_key = deepseek_api_key or os.environ.get(
            "DEEPSEEK_API_KEY"
        )
        self.forced_engine = forced_engine or os.environ.get("LLM_ENGINE")
        self.timeout_s = timeout_s

    def generate(self, prompt: str, system: Optional[str] = None) -> LLMResponse:
        """Sinh text từ prompt. Thử Gemini trước, fallback DeepSeek khi cần.

        Raises:
            LLMError: khi không có engine nào khả dụng/thành công.
        """
        engines_to_try = self._engine_order()
        last_error: Optional[Exception] = None

        for engine in engines_to_try:
            try:
                if engine == "gemini":
                    return self._call_gemini(prompt, system)
                if engine == "deepseek":
                    return self._call_deepseek(prompt, system)
            except Exception as exc:  # noqa: BLE001 - muốn bắt mọi lỗi để fallback
                logger.warning("LLM engine %s thất bại: %s", engine, exc)
                last_error = exc
                continue

        raise LLMError(
            f"Tất cả LLM engine đều thất bại (đã thử: {engines_to_try}). "
            f"Lỗi cuối: {last_error}"
        )

    def _engine_order(self) -> list[str]:
        if self.forced_engine in ("gemini", "deepseek"):
            return [self.forced_engine]
        order = []
        if self.gemini_api_key:
            order.append("gemini")
        if self.deepseek_api_key:
            order.append("deepseek")
        return order

    def _call_gemini(self, prompt: str, system: Optional[str]) -> LLMResponse:
        if not self.gemini_api_key:
            raise LLMError("Thiếu GEMINI_API_KEY")

        url = GEMINI_ENDPOINT.format(model=GEMINI_MODEL)
        contents = []
        if system:
            # Gemini generateContent dùng systemInstruction riêng.
            pass
        contents.append({"role": "user", "parts": [{"text": prompt}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {"temperature": DEFAULT_TEMPERATURE},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        resp = requests.post(
            url,
            params={"key": self.gemini_api_key},
            json=payload,
            timeout=self.timeout_s,
        )
        if resp.status_code == 429:
            raise LLMError("Gemini rate-limited (429)")
        resp.raise_for_status()
        data = resp.json()

        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Gemini response không đúng format: {data}") from exc

        usage = data.get("usageMetadata", {})
        return LLMResponse(
            text=text,
            engine="gemini",
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
        )

    def _call_deepseek(self, prompt: str, system: Optional[str]) -> LLMResponse:
        if not self.deepseek_api_key:
            raise LLMError("Thiếu DEEPSEEK_API_KEY")

        headers = {
            "x-api-key": self.deepseek_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": DEEPSEEK_MODEL,
            "max_tokens": 4096,
            "temperature": DEFAULT_TEMPERATURE,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system

        resp = requests.post(
            DEEPSEEK_ENDPOINT, headers=headers, json=payload, timeout=self.timeout_s
        )
        if resp.status_code == 429:
            raise LLMError("DeepSeek rate-limited (429)")
        resp.raise_for_status()
        data = resp.json()

        try:
            text = "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
        except (KeyError, IndexError) as exc:
            raise LLMError(f"DeepSeek response không đúng format: {data}") from exc

        usage = data.get("usage", {})
        return LLMResponse(
            text=text,
            engine="deepseek",
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )


if __name__ == "__main__":
    # Smoke test thủ công, không chạy trong CI bước 1.
    logging.basicConfig(level=logging.INFO)
    client = LLMClient()
    if not client._engine_order():
        print("Không có API key nào trong env (GEMINI_API_KEY/DEEPSEEK_API_KEY).")
    else:
        t0 = time.time()
        res = client.generate("Nói 'xin chào' bằng tiếng Việt, ngắn gọn.")
        print(f"[{res.engine}] {res.text} ({time.time() - t0:.1f}s)")

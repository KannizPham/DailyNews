"""Client LLM dùng Gemini 3.5 Flash làm engine chính và DeepSeek fallback.

Module gọi trực tiếp các REST API bằng ``requests``, hỗ trợ retry có giới hạn,
ẩn dữ liệu nhạy cảm trong lỗi/log và giữ một interface thống nhất cho pipeline.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests
from requests import exceptions as requests_exceptions

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)

DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL
DEEPSEEK_ENDPOINT = "https://api.deepseek.com/anthropic/v1/messages"

DEFAULT_TIMEOUT_S = 60
DEFAULT_TEMPERATURE = 0.2
DEFAULT_GEMINI_MAX_OUTPUT_TOKENS = 8192
DEFAULT_DEEPSEEK_MAX_OUTPUT_TOKENS = 4096
DEFAULT_MAX_ATTEMPTS = 3

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_MAX_ERROR_MESSAGE_LENGTH = 300


class LLMError(RuntimeError):
    """Lỗi an toàn khi một hoặc tất cả LLM engine thất bại."""


@dataclass
class LLMResponse:
    text: str
    engine: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


def sanitize_sensitive_text(value: object, *extra_secrets: object) -> str:
    """Ẩn API key và token phổ biến khỏi text dùng trong exception hoặc log."""
    text = str(value)

    known_secrets = [
        os.environ.get("GEMINI_API_KEY"),
        os.environ.get("GOOGLE_API_KEY"),
        os.environ.get("DEEPSEEK_API_KEY"),
        os.environ.get("TELEGRAM_BOT_TOKEN"),
        *extra_secrets,
    ]
    for secret in known_secrets:
        if secret is None:
            continue
        secret_text = str(secret).strip()
        if secret_text:
            text = text.replace(secret_text, "[REDACTED]")

    patterns = (
        (
            r"(?i)(\b(?:key|api[_-]?key|token)\s*=\s*)[^&#\s]+",
            r"\1[REDACTED]",
        ),
        (
            r"(?im)(\b(?:x-goog-api-key|x-api-key|authorization)\s*:\s*)"
            r"[^\r\n,;]+",
            r"\1[REDACTED]",
        ),
        (r"\bAIza[A-Za-z0-9_-]{16,}\b", "[REDACTED]"),
        (r"\bAQ\.[A-Za-z0-9_-]{16,}\b", "[REDACTED]"),
        (r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b", "[REDACTED]"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default

    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s không hợp lệ; dùng giá trị mặc định %d.",
            name,
            default,
        )
        return default

    if value < minimum:
        logger.warning(
            "%s phải >= %d; dùng giá trị mặc định %d.",
            name,
            minimum,
            default,
        )
        return default

    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default

    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "%s không hợp lệ; dùng giá trị mặc định %s.",
            name,
            default,
        )
        return default


def _optional_int(value: object) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _short_error_message(
    response: requests.Response,
    *secrets: object,
) -> str:
    message = ""

    try:
        data = response.json()
    except (ValueError, requests_exceptions.JSONDecodeError):
        data = None

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
        elif isinstance(error, str):
            message = error

    if not message and isinstance(response.reason, str):
        message = response.reason

    message = sanitize_sensitive_text(message, *secrets).strip()
    if not message:
        return "không có chi tiết"

    if len(message) > _MAX_ERROR_MESSAGE_LENGTH:
        return f"{message[:_MAX_ERROR_MESSAGE_LENGTH].rstrip()}…"

    return message


class LLMClient:
    """Gọi Gemini 3.5 Flash trước và fallback sang DeepSeek khi khả dụng.

    Có thể ép một engine bằng ``LLM_ENGINE=gemini|deepseek`` hoặc tham số
    ``forced_engine``. Khi không ép, chỉ những engine có API key mới được thử.
    """

    def __init__(
        self,
        gemini_api_key: Optional[str] = None,
        deepseek_api_key: Optional[str] = None,
        forced_engine: Optional[str] = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        explicit_gemini_key = (gemini_api_key or "").strip()
        env_gemini_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
        google_key = (os.environ.get("GOOGLE_API_KEY") or "").strip()
        self.gemini_api_key = (
            explicit_gemini_key
            or env_gemini_key
            or google_key
            or None
        )

        explicit_deepseek_key = (deepseek_api_key or "").strip()
        env_deepseek_key = (
            os.environ.get("DEEPSEEK_API_KEY") or ""
        ).strip()
        self.deepseek_api_key = (
            explicit_deepseek_key or env_deepseek_key or None
        )

        engine_value = (
            forced_engine
            if forced_engine is not None
            else os.environ.get("LLM_ENGINE")
        )
        self.forced_engine = (
            (engine_value or "").strip().lower() or None
        )

        if timeout_s == DEFAULT_TIMEOUT_S:
            self.timeout_s = _env_int(
                "LLM_TIMEOUT_S",
                DEFAULT_TIMEOUT_S,
            )
        elif timeout_s > 0:
            self.timeout_s = timeout_s
        else:
            self.timeout_s = DEFAULT_TIMEOUT_S

        self.temperature = _env_float(
            "LLM_TEMPERATURE",
            DEFAULT_TEMPERATURE,
        )
        self.gemini_max_output_tokens = _env_int(
            "GEMINI_MAX_OUTPUT_TOKENS",
            DEFAULT_GEMINI_MAX_OUTPUT_TOKENS,
        )
        self.deepseek_max_output_tokens = _env_int(
            "DEEPSEEK_MAX_OUTPUT_TOKENS",
            DEFAULT_DEEPSEEK_MAX_OUTPUT_TOKENS,
        )
        self.max_attempts = _env_int(
            "LLM_MAX_ATTEMPTS",
            DEFAULT_MAX_ATTEMPTS,
        )

    def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
    ) -> LLMResponse:
        """Sinh text, fallback engine và chỉ raise lỗi đã làm sạch."""
        engines_to_try = self._engine_order()
        if not engines_to_try:
            raise LLMError(
                "Không có LLM engine khả dụng; hãy cấu hình API key."
            )

        last_error = "không rõ nguyên nhân"

        for engine in engines_to_try:
            try:
                if engine == "gemini":
                    return self._call_gemini(prompt, system)
                if engine == "deepseek":
                    return self._call_deepseek(prompt, system)

                raise LLMError(
                    f"LLM engine không được hỗ trợ: {engine}"
                )
            except Exception as exc:
                safe_error = sanitize_sensitive_text(
                    exc,
                    self.gemini_api_key,
                    self.deepseek_api_key,
                )
                logger.warning(
                    "LLM engine %s thất bại: %s",
                    engine,
                    safe_error,
                )
                last_error = safe_error

        safe_final_error = sanitize_sensitive_text(
            last_error,
            self.gemini_api_key,
            self.deepseek_api_key,
        )
        raise LLMError(
            f"Tất cả LLM engine đều thất bại "
            f"(đã thử: {engines_to_try}). "
            f"Lỗi cuối: {safe_final_error}"
        )

    def _engine_order(self) -> list[str]:
        if self.forced_engine in ("gemini", "deepseek"):
            return [self.forced_engine]

        order: list[str] = []

        if self.gemini_api_key:
            order.append("gemini")
        if self.deepseek_api_key:
            order.append("deepseek")

        return order

    def _sleep_before_retry(
        self,
        engine: str,
        attempt: int,
        reason: str,
    ) -> None:
        delay_s = 2 ** (attempt - 1)
        safe_reason = sanitize_sensitive_text(
            reason,
            self.gemini_api_key,
            self.deepseek_api_key,
        )

        logger.warning(
            "%s lỗi tạm thời ở lần thử %d/%d (%s); "
            "thử lại sau %d giây.",
            engine,
            attempt,
            self.max_attempts,
            safe_reason,
            delay_s,
        )
        time.sleep(delay_s)

    def _post_with_retry(
        self,
        *,
        engine: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> requests.Response:
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout_s,
                )
            except (
                requests_exceptions.Timeout,
                requests_exceptions.ConnectionError,
            ) as exc:
                safe_error = sanitize_sensitive_text(
                    exc,
                    self.gemini_api_key,
                    self.deepseek_api_key,
                )

                if attempt < self.max_attempts:
                    self._sleep_before_retry(
                        engine,
                        attempt,
                        safe_error,
                    )
                    continue

                raise LLMError(
                    f"{engine} không kết nối được sau "
                    f"{self.max_attempts} lần thử: {safe_error}"
                ) from None
            except requests_exceptions.RequestException as exc:
                safe_error = sanitize_sensitive_text(
                    exc,
                    self.gemini_api_key,
                    self.deepseek_api_key,
                )
                raise LLMError(
                    f"{engine} request thất bại: {safe_error}"
                ) from None

            if (
                response.status_code in _RETRYABLE_STATUS_CODES
                and attempt < self.max_attempts
            ):
                self._sleep_before_retry(
                    engine,
                    attempt,
                    f"HTTP {response.status_code}",
                )
                continue

            return response

        raise LLMError(
            f"{engine} request thất bại sau "
            f"{self.max_attempts} lần thử."
        )

    def _call_gemini(
        self,
        prompt: str,
        system: Optional[str],
    ) -> LLMResponse:
        if not self.gemini_api_key:
            raise LLMError(
                "Thiếu GEMINI_API_KEY hoặc GOOGLE_API_KEY."
            )

        url = GEMINI_ENDPOINT.format(model=GEMINI_MODEL)
        headers = {
            "x-goog-api-key": self.gemini_api_key,
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                    ],
                }
            ],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.gemini_max_output_tokens,
            },
        }

        if system:
            payload["systemInstruction"] = {
                "parts": [
                    {"text": system},
                ]
            }

        response = self._post_with_retry(
            engine="Gemini",
            url=url,
            headers=headers,
            payload=payload,
        )
        self._raise_for_gemini_status(response)

        try:
            data = response.json()
        except (ValueError, requests_exceptions.JSONDecodeError):
            raise LLMError(
                "Gemini trả về response không phải JSON."
            ) from None

        if not isinstance(data, dict):
            raise LLMError(
                "Gemini trả về JSON không đúng định dạng object."
            )

        prompt_feedback = data.get("promptFeedback")
        if isinstance(prompt_feedback, dict):
            block_reason = prompt_feedback.get("blockReason")
            if isinstance(block_reason, str) and block_reason:
                safe_reason = sanitize_sensitive_text(
                    block_reason,
                    self.gemini_api_key,
                )
                raise LLMError(
                    f"Gemini đã chặn request: {safe_reason}."
                )

        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise LLMError(
                "Gemini không trả về candidate nào."
            )

        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise LLMError(
                "Gemini candidate không đúng định dạng."
            )

        finish_reason = candidate.get("finishReason")
        if (
            isinstance(finish_reason, str)
            and finish_reason not in ("", "STOP", "MAX_TOKENS")
        ):
            safe_reason = sanitize_sensitive_text(
                finish_reason,
                self.gemini_api_key,
            )
            raise LLMError(
                "Gemini dừng sinh nội dung bất thường: "
                f"{safe_reason}."
            )

        content = candidate.get("content")
        if not isinstance(content, dict):
            raise LLMError(
                "Gemini response không có content hợp lệ."
            )

        parts = content.get("parts")
        if not isinstance(parts, list):
            raise LLMError(
                "Gemini response không có parts hợp lệ."
            )

        text_parts: list[str] = []

        for part in parts:
            if isinstance(part, dict):
                value = part.get("text")
                if isinstance(value, str):
                    text_parts.append(value)

        text = "".join(text_parts).strip()
        if not text:
            raise LLMError(
                "Gemini trả về nội dung text rỗng."
            )

        usage = data.get("usageMetadata")
        if not isinstance(usage, dict):
            usage = {}

        return LLMResponse(
            text=text,
            engine="gemini",
            input_tokens=_optional_int(
                usage.get("promptTokenCount")
            ),
            output_tokens=_optional_int(
                usage.get("candidatesTokenCount")
            ),
        )

    def _raise_for_gemini_status(
        self,
        response: requests.Response,
    ) -> None:
        status = response.status_code

        if 200 <= status < 300:
            return

        detail = _short_error_message(
            response,
            self.gemini_api_key,
        )

        if status == 400:
            message = (
                f"Gemini request không hợp lệ: {detail}"
            )
        elif status in (401, 403):
            message = (
                "Gemini API key không hợp lệ, hết hiệu lực "
                "hoặc không có quyền truy cập model."
            )
        elif status == 404:
            message = (
                f"Gemini model '{GEMINI_MODEL}' không tồn tại "
                "hoặc chưa khả dụng cho project này."
            )
        elif status == 429:
            message = (
                "Gemini đang bị giới hạn quota hoặc rate limit."
            )
        elif status in (500, 502, 503, 504):
            message = (
                "Dịch vụ Gemini đang tạm thời gặp sự cố "
                f"(HTTP {status})."
            )
        else:
            message = (
                f"Gemini API lỗi HTTP {status}: {detail}"
            )

        raise LLMError(
            sanitize_sensitive_text(
                message,
                self.gemini_api_key,
            )
        )

    def _call_deepseek(
        self,
        prompt: str,
        system: Optional[str],
    ) -> LLMResponse:
        if not self.deepseek_api_key:
            raise LLMError("Thiếu DEEPSEEK_API_KEY.")

        headers = {
            "x-api-key": self.deepseek_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": DEEPSEEK_MODEL,
            "max_tokens": self.deepseek_max_output_tokens,
            "temperature": self.temperature,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        }

        if system:
            payload["system"] = system

        response = self._post_with_retry(
            engine="DeepSeek",
            url=DEEPSEEK_ENDPOINT,
            headers=headers,
            payload=payload,
        )
        self._raise_for_deepseek_status(response)

        try:
            data = response.json()
        except (ValueError, requests_exceptions.JSONDecodeError):
            raise LLMError(
                "DeepSeek trả về response không phải JSON."
            ) from None

        if not isinstance(data, dict):
            raise LLMError(
                "DeepSeek trả về JSON không đúng định dạng object."
            )

        content = data.get("content")
        if not isinstance(content, list):
            raise LLMError(
                "DeepSeek response không có content hợp lệ."
            )

        text_parts: list[str] = []

        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
            ):
                value = block.get("text")
                if isinstance(value, str):
                    text_parts.append(value)

        text = "".join(text_parts).strip()
        if not text:
            raise LLMError(
                "DeepSeek trả về nội dung text rỗng."
            )

        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = {}

        return LLMResponse(
            text=text,
            engine="deepseek",
            input_tokens=_optional_int(
                usage.get("input_tokens")
            ),
            output_tokens=_optional_int(
                usage.get("output_tokens")
            ),
        )

    def _raise_for_deepseek_status(
        self,
        response: requests.Response,
    ) -> None:
        status = response.status_code

        if 200 <= status < 300:
            return

        detail = _short_error_message(
            response,
            self.deepseek_api_key,
        )

        if status == 400:
            message = (
                f"DeepSeek request không hợp lệ: {detail}"
            )
        elif status in (401, 403):
            message = (
                "DeepSeek API key không hợp lệ, hết hiệu lực "
                "hoặc không có quyền truy cập model."
            )
        elif status == 404:
            message = (
                f"DeepSeek model '{DEEPSEEK_MODEL}' không tồn tại "
                "hoặc chưa khả dụng."
            )
        elif status == 429:
            message = (
                "DeepSeek đang bị giới hạn quota hoặc rate limit."
            )
        elif status in (500, 502, 503, 504):
            message = (
                "Dịch vụ DeepSeek đang tạm thời gặp sự cố "
                f"(HTTP {status})."
            )
        else:
            message = (
                f"DeepSeek API lỗi HTTP {status}: {detail}"
            )

        raise LLMError(
            sanitize_sensitive_text(
                message,
                self.deepseek_api_key,
            )
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = LLMClient()

    if not client._engine_order():
        print("Không có API key cho Gemini hoặc DeepSeek.")
    else:
        try:
            result = client.generate(
                "Trả lời đúng một từ: chào."
            )
            print(f"[{result.engine}] {result.text}")
        except LLMError as exc:
            print(
                sanitize_sensitive_text(
                    exc,
                    client.gemini_api_key,
                    client.deepseek_api_key,
                )
            )

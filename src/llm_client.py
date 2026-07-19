"""Gemini client tiết kiệm quota cho DailyNews.

Thứ tự model cố định:
1. gemini-3.5-flash
2. gemini-3.0-flash
3. gemini-2.5-flash

Ba model dùng chung ``GEMINI_API_KEY``. Mặc định mỗi model chỉ được gọi một
lần; lỗi quota, lỗi máy chủ hoặc model chưa khả dụng mới chuyển model kế tiếp.
Lỗi request/xác thực 400, 401 và 403 dừng ngay để không lãng phí quota.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

import requests
from requests import exceptions as requests_exceptions

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_MODELS = (
    DEFAULT_GEMINI_MODEL,
    "gemini-3.0-flash",
    "gemini-2.5-flash",
)
# Giữ alias cũ để các module import GEMINI_MODEL không bị vỡ.
GEMINI_MODEL = DEFAULT_GEMINI_MODEL
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)

DEFAULT_TIMEOUT_S = 45
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_OUTPUT_TOKENS = 4096
DEFAULT_MAX_ATTEMPTS = 1
DEFAULT_CACHE_SIZE = 16
MAX_CONFIGURED_ATTEMPTS = 3

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
NON_FALLBACK_STATUS_CODES = frozenset({400, 401, 403})
MAX_ERROR_DETAIL_LENGTH = 300


class LLMError(RuntimeError):
    """Lỗi an toàn; ``allow_fallback`` quyết định có thử model kế tiếp."""

    def __init__(self, message: str, *, allow_fallback: bool = True) -> None:
        super().__init__(message)
        self.allow_fallback = allow_fallback


@dataclass(frozen=True)
class LLMResponse:
    text: str
    engine: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


def sanitize_sensitive_text(value: object, *extra_secrets: object) -> str:
    """Che secret trước khi text đi vào log, exception hoặc Telegram."""
    safe = str(value)
    secrets = (
        os.environ.get("GEMINI_API_KEY"),
        os.environ.get("TELEGRAM_BOT_TOKEN"),
        *extra_secrets,
    )
    for secret in secrets:
        secret_text = str(secret or "").strip()
        if secret_text:
            safe = safe.replace(secret_text, "[REDACTED]")

    patterns = (
        (r"\bAIza[A-Za-z0-9_-]{16,}\b", "[REDACTED]"),
        (r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b", "[REDACTED]"),
        (
            r"(?i)(authorization|x-goog-api-key|api[_-]?key|token)"
            r"\s*[:=]\s*[^\s,;&]+",
            r"\1: [REDACTED]",
        ),
    )
    for pattern, replacement in patterns:
        safe = re.sub(pattern, replacement, safe)
    return safe


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s không hợp lệ; dùng %d.", name, default)
        return default
    if value < minimum or value > maximum:
        logger.warning("%s phải trong [%d, %d]; dùng %d.", name, minimum, maximum, default)
        return default
    return value


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s không hợp lệ; dùng %s.", name, default)
        return default


def _optional_int(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _response_detail(response: requests.Response, *secrets: object) -> str:
    detail = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            detail = error["message"]
        elif isinstance(error, str):
            detail = error
    if not detail:
        detail = str(getattr(response, "reason", "") or "không có chi tiết")
    return sanitize_sensitive_text(detail, *secrets).strip()[:MAX_ERROR_DETAIL_LENGTH]


def _join_text_parts(parts: object) -> str:
    if not isinstance(parts, list):
        return ""
    return "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


class LLMClient:
    """Gemini client đồng bộ với fallback model và cache trong tiến trình."""

    def __init__(
        self,
        gemini_api_key: Optional[str] = None,
        forced_engine: Optional[str] = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.gemini_api_key = (
            (gemini_api_key or "").strip()
            or (os.environ.get("GEMINI_API_KEY") or "").strip()
            or None
        )
        selected = forced_engine if forced_engine is not None else os.environ.get("LLM_ENGINE")
        self.forced_engine = (selected or "").strip().lower() or None
        self.timeout_s = timeout_s if timeout_s > 0 else DEFAULT_TIMEOUT_S
        self.temperature = _env_float("LLM_TEMPERATURE", DEFAULT_TEMPERATURE)
        self.max_output_tokens = _env_int(
            "LLM_MAX_OUTPUT_TOKENS",
            DEFAULT_MAX_OUTPUT_TOKENS,
            minimum=256,
            maximum=8192,
        )
        self.max_attempts = _env_int(
            "LLM_MAX_ATTEMPTS",
            DEFAULT_MAX_ATTEMPTS,
            minimum=1,
            maximum=MAX_CONFIGURED_ATTEMPTS,
        )
        self.cache_size = _env_int(
            "LLM_CACHE_SIZE",
            DEFAULT_CACHE_SIZE,
            minimum=0,
            maximum=128,
        )
        self._cache: OrderedDict[str, LLMResponse] = OrderedDict()

    def _model_order(self) -> list[str]:
        if not self.gemini_api_key:
            return []
        if self.forced_engine in (None, "gemini", DEFAULT_GEMINI_MODEL):
            return list(GEMINI_MODELS)
        if self.forced_engine in GEMINI_MODELS:
            return [self.forced_engine]
        return []

    def _cache_key(self, prompt: str, system: Optional[str]) -> str:
        material = json.dumps(
            {
                "prompt": prompt,
                "system": system or "",
                "temperature": self.temperature,
                "max_output_tokens": self.max_output_tokens,
                "models": self._model_order(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> Optional[LLMResponse]:
        if not self.cache_size or key not in self._cache:
            return None
        response = self._cache.pop(key)
        self._cache[key] = response
        logger.info("LLM cache hit [%s], không gọi API.", response.engine)
        return response

    def _cache_put(self, key: str, response: LLMResponse) -> None:
        if not self.cache_size:
            return
        self._cache[key] = response
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    def generate(self, prompt: str, system: Optional[str] = None) -> LLMResponse:
        if not isinstance(prompt, str) or not prompt.strip():
            raise LLMError("Prompt rỗng; không gọi API để tránh tốn quota.", allow_fallback=False)

        models = self._model_order()
        if not models:
            raise LLMError("Không có Gemini model khả dụng; hãy cấu hình GEMINI_API_KEY.")

        cache_key = self._cache_key(prompt, system)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        attempted: list[str] = []
        last_error = "không rõ nguyên nhân"
        for model in models:
            attempted.append(model)
            try:
                response = self._call_gemini(prompt, system, model)
                self._cache_put(cache_key, response)
                return response
            except Exception as exc:
                safe = sanitize_sensitive_text(exc, self.gemini_api_key)
                logger.warning("Gemini model %s thất bại: %s", model, safe)
                last_error = safe
                if isinstance(exc, LLMError) and not exc.allow_fallback:
                    raise LLMError(
                        f"{model} thất bại và không được phép fallback: {safe}",
                        allow_fallback=False,
                    ) from None

        raise LLMError(
            "Tất cả Gemini model đều thất bại "
            f"(đã thử: {', '.join(attempted)}). "
            f"Lỗi cuối: {sanitize_sensitive_text(last_error, self.gemini_api_key)}"
        )

    def _post(
        self,
        *,
        model: str,
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
            except (requests_exceptions.Timeout, requests_exceptions.ConnectionError) as exc:
                safe = sanitize_sensitive_text(exc, self.gemini_api_key)
                if attempt >= self.max_attempts:
                    raise LLMError(f"{model} không kết nối được: {safe}") from None
                time.sleep(2 ** (attempt - 1))
                continue
            except requests_exceptions.RequestException as exc:
                safe = sanitize_sensitive_text(exc, self.gemini_api_key)
                raise LLMError(f"{model} request thất bại: {safe}") from None

            # 429 chuyển model ngay; 5xx chỉ retry nếu operator chủ động tăng
            # LLM_MAX_ATTEMPTS. Mặc định 1 giúp tránh đốt quota/request.
            if response.status_code == 429:
                return response
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_attempts:
                time.sleep(2 ** (attempt - 1))
                continue
            return response
        raise LLMError(f"{model} request thất bại sau {self.max_attempts} lần thử.")

    def _call_gemini(self, prompt: str, system: Optional[str], model: str) -> LLMResponse:
        if not self.gemini_api_key:
            raise LLMError("Thiếu GEMINI_API_KEY.", allow_fallback=False)

        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_output_tokens,
            },
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        response = self._post(
            model=model,
            url=GEMINI_ENDPOINT.format(model=model),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.gemini_api_key,
            },
            payload=payload,
        )
        self._raise_for_gemini_status(response, model)

        try:
            data = response.json()
        except ValueError:
            raise LLMError(f"{model} trả response không phải JSON.") from None
        if not isinstance(data, dict):
            raise LLMError(f"{model} trả JSON không hợp lệ.")

        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise LLMError(f"{model} không trả candidate nào.")
        text = "".join(
            _join_text_parts(candidate.get("content", {}).get("parts"))
            for candidate in candidates
            if isinstance(candidate, dict) and isinstance(candidate.get("content"), dict)
        ).strip()
        if not text:
            raise LLMError(f"{model} trả nội dung rỗng.")

        usage = data.get("usageMetadata")
        usage = usage if isinstance(usage, dict) else {}
        return LLMResponse(
            text=text,
            engine=model,
            input_tokens=_optional_int(usage.get("promptTokenCount")),
            output_tokens=_optional_int(usage.get("candidatesTokenCount")),
        )

    def _raise_for_gemini_status(self, response: requests.Response, model: str) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return

        detail = _response_detail(response, self.gemini_api_key)
        unavailable = status == 404 or bool(
            re.search(
                r"(?i)(model[^.\n]*(?:not found|unavailable|not available)|"
                r"(?:not found|unavailable|not available)[^.\n]*model)",
                detail,
            )
        )
        if unavailable:
            raise LLMError(f"Gemini model {model} chưa khả dụng.")
        if status in NON_FALLBACK_STATUS_CODES:
            message = (
                f"Gemini request không hợp lệ: {detail}"
                if status == 400
                else "GEMINI_API_KEY không hợp lệ hoặc không có quyền truy cập."
            )
            raise LLMError(
                sanitize_sensitive_text(message, self.gemini_api_key),
                allow_fallback=False,
            )
        if status in RETRYABLE_STATUS_CODES:
            raise LLMError(f"{model} tạm thời lỗi hoặc hết quota (HTTP {status}).")
        raise LLMError(
            sanitize_sensitive_text(f"Gemini API lỗi HTTP {status}: {detail}", self.gemini_api_key),
            allow_fallback=False,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = LLMClient()
    try:
        result = client.generate("Trả lời đúng một từ: chào.")
        print(f"[{result.engine}] {result.text}")
    except LLMError as exc:
        print(sanitize_sensitive_text(exc, client.gemini_api_key))



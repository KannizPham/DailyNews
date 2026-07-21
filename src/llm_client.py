"""Gemini client với model discovery, fallback và giới hạn quota rõ ràng.

Model không được hardcode. Client gọi ``models.list`` bằng cùng API key, chỉ
giữ model Gemini hỗ trợ ``generateContent`` rồi cache danh sách trong instance.
``GEMINI_MODELS`` (phân tách bằng dấu phẩy) chỉ là thứ tự ưu tiên; nếu không
cấu hình, client chọn từ kết quả discovery với Flash/stable được ưu tiên.
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

GEMINI_MODELS_ENV = "GEMINI_MODELS"
GEMINI_MODELS_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_GENERATE_ENDPOINT = (
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
PREVIEW_MARKERS = ("preview", "experimental", "exp", "latest")


class LLMError(RuntimeError):
    """Lỗi đã sanitize; ``allow_fallback`` quyết định có thử model kế tiếp."""

    def __init__(
        self,
        message: str,
        *,
        allow_fallback: bool = True,
        model: Optional[str] = None,
        status: Optional[int] = None,
        error_code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.allow_fallback = allow_fallback
        self.model = model
        self.status = status
        self.error_code = error_code


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
        os.environ.get("CLOUDFLARE_API_TOKEN"),
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


def _normalize_model_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if name.startswith("models/"):
        name = name[len("models/") :]
    return name


def _parse_configured_models(raw: Optional[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in (raw or "").split(","):
        name = _normalize_model_name(value)
        if name and name not in seen:
            result.append(name)
            seen.add(name)
    return result


def _auto_model_sort_key(model: str) -> tuple[int, int, str]:
    lowered = model.lower()
    flash_rank = 0 if "flash" in lowered else 1
    preview_rank = 1 if any(marker in lowered for marker in PREVIEW_MARKERS) else 0
    return flash_rank, preview_rank, lowered


class LLMClient:
    """Gemini client đồng bộ với discovery, fallback và cache trong tiến trình."""

    def __init__(
        self,
        gemini_api_key: Optional[str] = None,
        forced_engine: Optional[str] = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        configured_models: Optional[str | list[str]] = None,
    ) -> None:
        self.gemini_api_key = (
            (gemini_api_key or "").strip()
            or (os.environ.get("GEMINI_API_KEY") or "").strip()
            or None
        )
        if isinstance(configured_models, list):
            configured = [_normalize_model_name(value) for value in configured_models]
            self.configured_models = list(dict.fromkeys(value for value in configured if value))
        else:
            raw_models = configured_models if configured_models is not None else os.environ.get(GEMINI_MODELS_ENV)
            self.configured_models = _parse_configured_models(raw_models)
        forced = _normalize_model_name(forced_engine)
        if forced and forced.lower() != "gemini":
            self.configured_models = [forced]

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
        self.cache_size = _env_int("LLM_CACHE_SIZE", DEFAULT_CACHE_SIZE, minimum=0, maximum=128)
        self._cache: OrderedDict[str, LLMResponse] = OrderedDict()
        self._discovered_model_order: Optional[list[str]] = None

    @property
    def has_api_key(self) -> bool:
        return bool(self.gemini_api_key)

    def _model_order(self) -> list[str]:
        if not self.gemini_api_key:
            return []
        if self._discovered_model_order is None:
            self._discovered_model_order = self._discover_models()
        return list(self._discovered_model_order)

    def _discover_models(self) -> list[str]:
        """Gọi models.list một lần và trả thứ tự model dùng cho process hiện tại."""
        if not self.gemini_api_key:
            return []

        available: list[str] = []
        page_token: Optional[str] = None
        while True:
            params = {"pageToken": page_token} if page_token else None
            try:
                response = requests.get(
                    GEMINI_MODELS_ENDPOINT,
                    headers={"x-goog-api-key": self.gemini_api_key},
                    params=params,
                    timeout=self.timeout_s,
                )
            except requests_exceptions.RequestException as exc:
                safe = sanitize_sensitive_text(exc, self.gemini_api_key)
                raise LLMError(
                    f"Gemini models.list không kết nối được: {safe}",
                    allow_fallback=False,
                    error_code="model_discovery_network",
                ) from None

            if not 200 <= response.status_code < 300:
                detail = _response_detail(response, self.gemini_api_key)
                raise LLMError(
                    f"Gemini models.list lỗi HTTP {response.status_code}: {detail}",
                    allow_fallback=False,
                    status=response.status_code,
                    error_code="model_discovery_http",
                )
            try:
                payload = response.json()
            except ValueError:
                raise LLMError(
                    "Gemini models.list trả response không phải JSON.",
                    allow_fallback=False,
                    error_code="model_discovery_invalid_json",
                ) from None
            if not isinstance(payload, dict):
                raise LLMError(
                    "Gemini models.list trả JSON không hợp lệ.",
                    allow_fallback=False,
                    error_code="model_discovery_invalid_json",
                )

            for entry in payload.get("models", []):
                if not isinstance(entry, dict):
                    continue
                methods = entry.get("supportedGenerationMethods")
                name = _normalize_model_name(entry.get("name"))
                if (
                    name
                    and name.lower().startswith("gemini-")
                    and isinstance(methods, list)
                    and "generateContent" in methods
                    and name not in available
                ):
                    available.append(name)

            raw_next_token = payload.get("nextPageToken")
            page_token = raw_next_token.strip() if isinstance(raw_next_token, str) else None
            if not page_token:
                break

        if self.configured_models:
            available_set = set(available)
            selected = [model for model in self.configured_models if model in available_set]
            if not selected:
                raise LLMError(
                    "Không có model nào trong GEMINI_MODELS xuất hiện trong models.list "
                    "và hỗ trợ generateContent.",
                    allow_fallback=False,
                    error_code="no_configured_model_available",
                )
        else:
            selected = sorted(available, key=_auto_model_sort_key)
            if not selected:
                raise LLMError(
                    "models.list không trả model Gemini nào hỗ trợ generateContent.",
                    allow_fallback=False,
                    error_code="no_model_available",
                )

        logger.info("Gemini models.list: model được chọn theo thứ tự: %s", ", ".join(selected))
        return selected

    def _cache_key(self, prompt: str, system: Optional[str], models: list[str]) -> str:
        material = json.dumps(
            {
                "prompt": prompt,
                "system": system or "",
                "temperature": self.temperature,
                "max_output_tokens": self.max_output_tokens,
                "models": models,
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
        if not self.gemini_api_key:
            raise LLMError(
                "Thiếu GEMINI_API_KEY; không thể gọi Gemini.",
                allow_fallback=False,
                error_code="missing_api_key",
            )

        models = self._model_order()
        cache_key = self._cache_key(prompt, system, models)
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
            except Exception as exc:  # exception luôn được sanitize trước khi thoát
                safe = sanitize_sensitive_text(exc, self.gemini_api_key)
                logger.warning("Gemini model %s thất bại: %s", model, safe)
                last_error = safe
                if isinstance(exc, LLMError) and not exc.allow_fallback:
                    raise LLMError(
                        f"Gemini dừng tại model {model}: {safe}",
                        allow_fallback=False,
                        model=model,
                        status=exc.status,
                        error_code=exc.error_code,
                    ) from None

        raise LLMError(
            "Tất cả Gemini model khả dụng đều thất bại "
            f"(đã thử: {', '.join(attempted)}). Lỗi cuối: {last_error}",
            error_code="all_models_failed",
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
                    raise LLMError(
                        f"model={model} HTTP=network: {safe}",
                        model=model,
                        error_code="network_error",
                    ) from None
                time.sleep(2 ** (attempt - 1))
                continue
            except requests_exceptions.RequestException as exc:
                safe = sanitize_sensitive_text(exc, self.gemini_api_key)
                raise LLMError(
                    f"model={model} HTTP=network: {safe}",
                    model=model,
                    error_code="request_error",
                ) from None

            # 429 chuyển model ngay; chỉ 5xx retry nếu operator tăng attempts.
            if response.status_code == 429:
                return response
            if response.status_code in {500, 502, 503, 504} and attempt < self.max_attempts:
                time.sleep(2 ** (attempt - 1))
                continue
            return response
        raise LLMError(f"model={model} request thất bại sau {self.max_attempts} lần thử.")

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
            url=GEMINI_GENERATE_ENDPOINT.format(model=model),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.gemini_api_key},
            payload=payload,
        )
        self._raise_for_gemini_status(response, model)

        try:
            data = response.json()
        except ValueError:
            raise LLMError(f"model={model} HTTP=200: response không phải JSON.", model=model) from None
        if not isinstance(data, dict):
            raise LLMError(f"model={model} HTTP=200: JSON không hợp lệ.", model=model)

        prompt_feedback = data.get("promptFeedback")
        block_reason = prompt_feedback.get("blockReason") if isinstance(prompt_feedback, dict) else None
        candidates = data.get("candidates")
        safety_finish = any(
            isinstance(candidate, dict) and candidate.get("finishReason") == "SAFETY"
            for candidate in (candidates if isinstance(candidates, list) else [])
        )
        if block_reason or safety_finish:
            reason = sanitize_sensitive_text(block_reason or "SAFETY", self.gemini_api_key)
            raise LLMError(
                f"model={model} HTTP=200: safety block ({reason}).",
                allow_fallback=False,
                model=model,
                status=200,
                error_code="safety_block",
            )
        if not isinstance(candidates, list) or not candidates:
            raise LLMError(f"model={model} HTTP=200: không có candidate.", model=model)

        text = "".join(
            _join_text_parts(candidate.get("content", {}).get("parts"))
            for candidate in candidates
            if isinstance(candidate, dict) and isinstance(candidate.get("content"), dict)
        ).strip()
        if not text:
            raise LLMError(f"model={model} HTTP=200: nội dung rỗng.", model=model)

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
        message = f"model={model} HTTP={status}: {detail}"
        if status == 404:
            raise LLMError(message, model=model, status=status, error_code="model_unavailable")
        if status in NON_FALLBACK_STATUS_CODES:
            error_code = "bad_request" if status == 400 else "auth_error"
            raise LLMError(
                message,
                allow_fallback=False,
                model=model,
                status=status,
                error_code=error_code,
            )
        if status in RETRYABLE_STATUS_CODES:
            raise LLMError(message, model=model, status=status, error_code="provider_unavailable")
        raise LLMError(
            message,
            allow_fallback=False,
            model=model,
            status=status,
            error_code="provider_http_error",
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = LLMClient()
    try:
        result = client.generate("Trả lời đúng một từ: chào.")
        print(f"[{result.engine}] {result.text}")
    except LLMError as exc:
        print(sanitize_sensitive_text(exc, client.gemini_api_key))

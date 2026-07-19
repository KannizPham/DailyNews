from __future__ import annotations

import logging
from pathlib import Path
import sys

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import llm_client


class FakeResponse:
    def __init__(self, status_code: int, payload: dict, reason: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.reason = reason

    def json(self) -> dict:
        return self._payload


def gemini_success(*parts: str) -> FakeResponse:
    return FakeResponse(
        200,
        {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": part} for part in parts]},
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 3,
                "candidatesTokenCount": 4,
            },
        },
    )


def api_error(status: int, message: str) -> FakeResponse:
    return FakeResponse(status, {"error": {"message": message}})


def test_gemini_35_success_does_not_call_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return gemini_success("xin ", "chào")

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    client = llm_client.LLMClient(gemini_api_key="gemini-secret")
    client.max_attempts = 1

    result = client.generate("hello")

    assert result.text == "xin chào"
    assert result.engine == "gemini-3.5-flash"
    assert len(calls) == 1
    assert "gemini-3.5-flash" in calls[0]["url"]
    assert "gemini-3.0-flash" not in calls[0]["url"]
    assert "gemini-2.5-flash" not in calls[0]["url"]
    assert "gemini-secret" not in calls[0]["url"]
    assert calls[0]["headers"]["x-goog-api-key"] == "gemini-secret"


def test_gemini_35_429_then_30_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [api_error(429, "rate limited"), gemini_success("fallback ok")]
    urls: list[str] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        urls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    client = llm_client.LLMClient(gemini_api_key="gemini-secret")
    client.max_attempts = 1

    result = client.generate("hello")

    assert result.engine == "gemini-3.0-flash"
    assert "gemini-3.5-flash" in urls[0]
    assert "gemini-3.0-flash" in urls[1]


def test_gemini_35_and_30_fail_then_25_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        api_error(503, "temporarily unavailable"),
        api_error(429, "rate limited"),
        gemini_success("2.5 fallback ok"),
    ]
    urls: list[str] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        urls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    client = llm_client.LLMClient(gemini_api_key="gemini-secret")
    client.max_attempts = 1

    result = client.generate("hello")

    assert result.engine == "gemini-2.5-flash"
    assert [model in url for model, url in zip(llm_client.GEMINI_MODELS, urls)] == [True] * 3


def test_gemini_model_not_found_then_25_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [api_error(404, "model gemini-3.5-flash not found"), gemini_success("fallback")]

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        return responses.pop(0)

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    client = llm_client.LLMClient(gemini_api_key="gemini-secret")
    client.max_attempts = 1

    result = client.generate("hello")

    assert result.engine == "gemini-3.0-flash"


def test_gemini_malformed_request_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        calls.append(url)
        return api_error(400, "malformed request")

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    client = llm_client.LLMClient(gemini_api_key="gemini-secret")
    client.max_attempts = 1

    with pytest.raises(llm_client.LLMError):
        client.generate("hello")

    assert len(calls) == 1
    assert "gemini-3.5-flash" in calls[0]


def test_all_gemini_models_fail_without_external_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        calls.append(url)
        return api_error(503, "temporarily unavailable")

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    client = llm_client.LLMClient(gemini_api_key="gemini-secret")
    client.max_attempts = 1

    with pytest.raises(llm_client.LLMError) as exc_info:
        client.generate("hello")

    assert "Tất cả Gemini model đều thất bại" in str(exc_info.value)
    assert "gemini-3.5-flash" in calls[0]
    assert "gemini-3.0-flash" in calls[1]
    assert "gemini-2.5-flash" in calls[2]
    assert len(calls) == 3


def test_secrets_never_appear_in_returned_errors_or_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secrets = ("gemini-secret",)

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        return api_error(503, "leaked: " + " ".join(secrets))

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    client = llm_client.LLMClient(gemini_api_key=secrets[0])
    client.max_attempts = 1

    with caplog.at_level(logging.WARNING), pytest.raises(llm_client.LLMError) as exc_info:
        client.generate("hello")

    returned_error = str(exc_info.value)
    for secret in secrets:
        assert secret not in returned_error
        assert secret not in caplog.text


def test_gemini_auth_error_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        calls.append(url)
        return api_error(401, "bad key")

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    client = llm_client.LLMClient(gemini_api_key="gemini-secret")
    client.max_attempts = 1

    with pytest.raises(llm_client.LLMError):
        client.generate("hello")

    assert len(calls) == 1
    assert "gemini-3.5-flash" in calls[0]


def test_429_switches_model_immediately_without_retrying_same_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [api_error(429, "quota exceeded"), gemini_success("3.0 ok")]
    urls: list[str] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        urls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    client = llm_client.LLMClient(gemini_api_key="gemini-secret")
    client.max_attempts = 3

    result = client.generate("hello")

    assert result.engine == "gemini-3.0-flash"
    assert len(urls) == 2
    assert "gemini-3.5-flash" in urls[0]
    assert "gemini-3.0-flash" in urls[1]


def test_identical_prompt_uses_in_process_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        return gemini_success("cached")

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    client = llm_client.LLMClient(gemini_api_key="gemini-secret")

    first = client.generate("same prompt", system="same system")
    second = client.generate("same prompt", system="same system")

    assert first == second
    assert calls == 1


def test_default_output_limit_is_quota_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads: list[dict] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        payloads.append(kwargs["json"])
        return gemini_success("ok")

    monkeypatch.delenv("LLM_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    client = llm_client.LLMClient(gemini_api_key="gemini-secret")

    client.generate("hello")

    assert payloads[0]["generationConfig"]["maxOutputTokens"] == 4096

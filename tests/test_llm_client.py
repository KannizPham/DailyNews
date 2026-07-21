from __future__ import annotations

import logging
from pathlib import Path
import sys

import pytest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import llm_client  # noqa: E402


MODEL_A = "gemini-test-flash"
MODEL_B = "gemini-test-pro"


class FakeResponse:
    def __init__(self, status_code: int, payload: dict, reason: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.reason = reason
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def model_list(*entries: tuple[str, list[str]]) -> FakeResponse:
    return FakeResponse(
        200,
        {
            "models": [
                {"name": f"models/{name}", "supportedGenerationMethods": methods}
                for name, methods in entries
            ]
        },
    )


def gemini_success(text: str = "ok") -> FakeResponse:
    return FakeResponse(
        200,
        {
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4},
        },
    )


def api_error(status: int, message: str) -> FakeResponse:
    return FakeResponse(status, {"error": {"message": message}})


def install_discovery(monkeypatch: pytest.MonkeyPatch, response: FakeResponse | None = None) -> list[dict]:
    calls: list[dict] = []

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return response or model_list(
            (MODEL_A, ["generateContent"]),
            (MODEL_B, ["generateContent"]),
        )

    monkeypatch.setattr(llm_client.requests, "get", fake_get)
    return calls


def make_client() -> llm_client.LLMClient:
    client = llm_client.LLMClient(
        gemini_api_key="gemini-secret",
        configured_models=[MODEL_A, MODEL_B],
    )
    client.max_attempts = 1
    return client


def test_configured_first_model_success_does_not_call_second(monkeypatch: pytest.MonkeyPatch) -> None:
    get_calls = install_discovery(monkeypatch)
    post_calls: list[dict] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        post_calls.append({"url": url, **kwargs})
        return gemini_success("first")

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    result = make_client().generate("hello")

    assert result.engine == MODEL_A
    assert len(get_calls) == 1
    assert len(post_calls) == 1
    assert MODEL_A in post_calls[0]["url"]
    assert MODEL_B not in post_calls[0]["url"]
    assert "gemini-secret" not in get_calls[0]["url"]
    assert "gemini-secret" not in post_calls[0]["url"]
    assert get_calls[0]["headers"]["x-goog-api-key"] == "gemini-secret"
    assert post_calls[0]["headers"]["x-goog-api-key"] == "gemini-secret"


def test_429_switches_to_next_model_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    install_discovery(monkeypatch)
    responses = [api_error(429, "quota"), gemini_success("fallback")]
    urls: list[str] = []

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        urls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    client = make_client()
    client.max_attempts = 3
    result = client.generate("hello")

    assert result.engine == MODEL_B
    assert len(urls) == 2
    assert MODEL_A in urls[0] and MODEL_B in urls[1]


def test_404_switches_to_next_model(monkeypatch: pytest.MonkeyPatch) -> None:
    install_discovery(monkeypatch)
    responses = [api_error(404, "not found"), gemini_success("fallback")]
    monkeypatch.setattr(llm_client.requests, "post", lambda *args, **kwargs: responses.pop(0))
    assert make_client().generate("hello").engine == MODEL_B


def test_5xx_retries_only_to_configured_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    install_discovery(monkeypatch)
    monkeypatch.setattr(llm_client.time, "sleep", lambda _seconds: None)
    responses = [api_error(503, "temporary"), gemini_success("retry ok")]
    calls = 0

    def fake_post(*args: object, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        return responses.pop(0)

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    client = make_client()
    client.max_attempts = 2
    result = client.generate("hello")

    assert result.engine == MODEL_A
    assert calls == 2


def test_400_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    install_discovery(monkeypatch)
    calls = 0

    def fake_post(*args: object, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        return api_error(400, "malformed")

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    with pytest.raises(llm_client.LLMError) as exc_info:
        make_client().generate("hello")
    assert calls == 1
    assert "HTTP=400" in str(exc_info.value)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_do_not_fallback(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    install_discovery(monkeypatch)
    calls = 0

    def fake_post(*args: object, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        return api_error(status, "bad key")

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    with pytest.raises(llm_client.LLMError):
        make_client().generate("hello")
    assert calls == 1


def test_models_list_keeps_only_generate_content_models(monkeypatch: pytest.MonkeyPatch) -> None:
    install_discovery(
        monkeypatch,
        model_list(
            ("gemini-test-embed", ["embedContent"]),
            (MODEL_A, ["generateContent"]),
            ("other-generator", ["generateContent"]),
        ),
    )
    client = llm_client.LLMClient(
        gemini_api_key="gemini-secret",
        configured_models=["gemini-test-embed", MODEL_A],
    )
    assert client._model_order() == [MODEL_A]
    assert client._model_order() == [MODEL_A]  # cache: không gọi models.list lần hai


def test_auto_selection_prefers_flash_and_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    install_discovery(
        monkeypatch,
        model_list(
            ("gemini-test-pro-preview", ["generateContent"]),
            (MODEL_B, ["generateContent"]),
            ("gemini-test-flash-preview", ["generateContent"]),
            (MODEL_A, ["generateContent"]),
        ),
    )
    client = llm_client.LLMClient(gemini_api_key="gemini-secret", configured_models=[])
    assert client._model_order() == [
        MODEL_A,
        "gemini-test-flash-preview",
        MODEL_B,
        "gemini-test-pro-preview",
    ]


def test_no_configured_model_in_models_list_is_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    install_discovery(monkeypatch, model_list((MODEL_B, ["generateContent"])))
    client = llm_client.LLMClient(
        gemini_api_key="gemini-secret",
        configured_models=[MODEL_A],
    )
    with pytest.raises(llm_client.LLMError) as exc_info:
        client._model_order()
    assert "GEMINI_MODELS" in str(exc_info.value)
    assert exc_info.value.error_code == "no_configured_model_available"


def test_secrets_never_appear_in_errors_or_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    install_discovery(monkeypatch)
    monkeypatch.setattr(
        llm_client.requests,
        "post",
        lambda *args, **kwargs: api_error(503, "leaked gemini-secret"),
    )
    with caplog.at_level(logging.WARNING), pytest.raises(llm_client.LLMError) as exc_info:
        make_client().generate("hello")
    assert "gemini-secret" not in str(exc_info.value)
    assert "gemini-secret" not in caplog.text


def test_safety_block_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    install_discovery(monkeypatch)
    calls = 0

    def fake_post(*args: object, **kwargs: object) -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse(200, {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []})

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    with pytest.raises(llm_client.LLMError) as exc_info:
        make_client().generate("hello")
    assert calls == 1
    assert exc_info.value.error_code == "safety_block"

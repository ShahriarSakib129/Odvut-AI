"""Groq client tests with a fake HTTP session (no network access needed)."""

from __future__ import annotations

import json

import pytest
import requests

from ai.groq_client import GroqClient
from config import load_settings


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {})
        self.content = self.text.encode()

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Records requests and replays queued responses/exceptions."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "data": data, "timeout": timeout})
        result = self.responses.pop(0) if self.responses else FakeResponse(500, {"error": "x"})
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, url, headers=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "timeout": timeout})
        result = self.responses.pop(0) if self.responses else FakeResponse(500, {})
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture()
def settings():
    return load_settings({
        "BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
        "GROQ_API_KEY": "gsk_test_key_123456",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "GROQ_MODEL": "openai/gpt-oss-120b",
        "GROQ_MAX_RETRIES": "1",
        "GROQ_TIMEOUT_SECONDS": "5",
    })


def completion(text: str = "BTC এখন ধৈর্য ধরো।") -> dict:
    return {
        "id": "chatcmpl-1",
        "model": "openai/gpt-oss-120b",
        "choices": [{"message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 300, "completion_tokens": 60, "total_tokens": 360},
    }


def test_successful_completion(settings):
    session = FakeSession([FakeResponse(200, completion())])
    client = GroqClient(settings, session=session)
    result = client.chat([{"role": "user", "content": "BTC?"}])
    assert result.ok and result.text.startswith("BTC")
    assert result.total_tokens == 360
    assert result.model == "openai/gpt-oss-120b"
    assert session.requests[0]["headers"]["Authorization"].startswith("Bearer gsk_")


def test_invalid_key_is_not_retried(settings):
    session = FakeSession([FakeResponse(401, {"error": "invalid api key"})])
    client = GroqClient(settings, session=session)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.failed and result.error_kind == "invalid_key"
    assert len(session.requests) == 1, "4xx (non-retryable) must not be retried"


def test_rate_limit_is_retried_then_reported(settings):
    session = FakeSession([FakeResponse(429, {"error": "rate limited"}),
                           FakeResponse(429, {"error": "rate limited"})])
    client = GroqClient(settings, session=session)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.error_kind == "rate_limited"
    assert len(session.requests) == 2  # initial + one retry


def test_rate_limit_recovers_on_retry(settings):
    session = FakeSession([FakeResponse(429, {"error": "slow down"}),
                           FakeResponse(200, completion("ঠিক আছে"))])
    client = GroqClient(settings, session=session)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.ok and result.text == "ঠিক আছে"
    assert result.attempts == 2


def test_timeout_maps_to_timeout_kind(settings):
    session = FakeSession([requests.exceptions.Timeout(), requests.exceptions.Timeout()])
    client = GroqClient(settings, session=session)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.error_kind == "timeout"


def test_network_error_maps_to_network_kind(settings):
    session = FakeSession([requests.exceptions.ConnectionError("no route"),
                           requests.exceptions.ConnectionError("no route")])
    client = GroqClient(settings, session=session)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.error_kind == "network"


def test_model_unavailable(settings):
    session = FakeSession([FakeResponse(404, {"error": {"message": "model not found"}})])
    client = GroqClient(settings, session=session)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.error_kind == "model_unavailable"


def test_empty_completion_is_an_error(settings):
    session = FakeSession([FakeResponse(200, {"choices": [{"message": {"content": ""}}]})])
    client = GroqClient(settings, session=session)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.failed and result.error_kind == "empty"


def test_malformed_json_is_handled(settings):
    response = FakeResponse(200, None, text="<html>not json</html>")
    session = FakeSession([response])
    client = GroqClient(settings, session=session)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.failed and result.error_kind == "api_error"


def test_disabled_without_key():
    settings = load_settings({"BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
                              "DATABASE_URL": "postgresql://u:p@localhost/db",
                              "GROQ_API_KEY": ""})
    client = GroqClient(settings, session=FakeSession([]))
    assert client.enabled is False
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result.error_kind == "no_ai"


def test_groq_enabled_false_forces_disabled(settings):
    settings = load_settings({**{"BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
                                 "DATABASE_URL": "postgresql://u:p@localhost/db",
                                 "GROQ_API_KEY": "gsk_x"},
                              "GROQ_ENABLED": "false"})
    assert GroqClient(settings).enabled is False


def test_health_check_reports_model_availability(settings):
    payload = {"data": [{"id": "openai/gpt-oss-120b"}, {"id": "llama-3.3-70b-versatile"}]}
    session = FakeSession([FakeResponse(200, payload)])
    client = GroqClient(settings, session=session)
    status = client.health_check()
    assert status["ok"] is True
    assert status["model_available"] is True


def test_health_check_handles_bad_key(settings):
    session = FakeSession([FakeResponse(401, {"error": "unauthorized"})])
    client = GroqClient(settings, session=session)
    status = client.health_check()
    assert status["ok"] is False and status["error_kind"] == "invalid_key"


def test_payload_is_openai_compatible(settings):
    session = FakeSession([FakeResponse(200, completion())])
    client = GroqClient(settings, session=session)
    client.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
                temperature=0.2, max_tokens=100)
    payload = json.loads(session.requests[0]["data"])
    assert payload["model"] == "openai/gpt-oss-120b"
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 100
    assert payload["stream"] is False
    assert payload["messages"][0]["role"] == "system"


def test_api_key_is_never_logged(settings, logger_stream):
    stream, logger = logger_stream
    from utils.logger import register_secret

    register_secret(settings.groq_api_key)
    session = FakeSession([FakeResponse(401, {"error": "bad key gsk_test_key_123456"})])
    client = GroqClient(settings, session=session)
    logger.info("groq key is %s", settings.groq_api_key)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert "gsk_test_key_123456" not in stream.getvalue()
    assert "gsk_test_key_123456" not in result.short_error(500)

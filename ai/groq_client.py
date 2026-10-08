"""Groq API client (OpenAI-compatible chat completions).

Design goals (requirement #20 and #19):

* never crash the bot: every failure mode is mapped to a typed :class:`GroqError`
* never log the API key
* retry only on retryable conditions (timeouts, 429, 5xx) with backoff
* keep a short ``requests`` timeout so a Render free instance stays responsive
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import requests

from config import Settings
from utils.logger import get_logger, redact

logger = get_logger(__name__)

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
ERROR_KIND_BY_STATUS = {
    400: "bad_request",
    401: "invalid_key",
    403: "forbidden",
    404: "model_unavailable",
    413: "too_large",
    422: "bad_request",
    429: "rate_limited",
}


@dataclass
class AIResult:
    """Result of a single completion request."""

    ok: bool
    text: str = ""
    error: str = ""
    error_kind: str = "none"
    status_code: int | None = None
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 1
    finish_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return not self.ok

    def short_error(self, limit: int = 300) -> str:
        return redact(self.error)[:limit]


class GroqError(RuntimeError):
    """Raised only for programmer errors (never for API failures)."""


class GroqClient:
    """Thin, resilient wrapper around ``POST /openai/v1/chat/completions``."""

    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.settings = settings
        self.model = settings.groq_model
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._last_status: dict[str, Any] = {"ok": None, "checked_at": 0.0}
        self._last_error: str = ""

    # ------------------------------------------------------------------ #
    # availability
    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return bool(self.settings.groq_enabled and self.settings.groq_api_key)

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.groq_api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"{self.settings.bot_name.replace(' ', '-')}/1.0",
        }

    def health_check(self, *, timeout: float | None = None,
                     cache_seconds: float = 300.0) -> dict[str, Any]:
        """Cheap availability probe used by ``/status`` and the admin panel."""
        now = time.time()
        with self._lock:
            last = dict(self._last_status)
        if last.get("checked_at") and now - last["checked_at"] < cache_seconds and last.get("ok"):
            return last
        if not self.settings.groq_api_key:
            return {"ok": False, "error": "GROQ_API_KEY not set", "error_kind": "no_ai",
                    "checked_at": now}
        if not self.settings.groq_enabled:
            return {"ok": False, "error": "AI disabled by GROQ_ENABLED=false",
                    "error_kind": "no_ai", "checked_at": now}

        try:
            response = self._session.get(
                self.settings.groq_models_url,
                headers={"Authorization": f"Bearer {self.settings.groq_api_key}"},
                timeout=timeout or min(self.settings.groq_timeout_seconds, 15),
            )
        except requests.exceptions.Timeout:
            result = {"ok": False, "error": "timeout while contacting Groq",
                      "error_kind": "timeout", "checked_at": now}
        except requests.exceptions.RequestException as exc:
            result = {"ok": False, "error": redact(str(exc)), "error_kind": "network",
                      "checked_at": now}
        else:
            if response.status_code == 200:
                models: list[str] = []
                try:
                    payload = response.json()
                    models = [m.get("id", "") for m in payload.get("data", [])]
                except Exception:
                    pass
                result = {
                    "ok": True,
                    "status_code": 200,
                    "models": models[:50],
                    "model_available": (self.model in models) if models else None,
                    "error": "",
                    "error_kind": "none",
                    "checked_at": now,
                }
            else:
                kind = ERROR_KIND_BY_STATUS.get(response.status_code, "api_error")
                result = {
                    "ok": False,
                    "status_code": response.status_code,
                    "error": f"Groq /models returned {response.status_code}",
                    "error_kind": kind,
                    "checked_at": now,
                }
        with self._lock:
            self._last_status = result
            if not result.get("ok"):
                self._last_error = str(result.get("error") or "")
        return result

    # ------------------------------------------------------------------ #
    # completions
    # ------------------------------------------------------------------ #
    def chat(self, messages: Sequence[Mapping[str, str]], *, model: str | None = None,
             temperature: float | None = None, max_tokens: int | None = None,
             top_p: float | None = None, stop: Sequence[str] | None = None) -> AIResult:
        """Run one chat completion with retries. Never raises for API problems."""
        if not self.settings.groq_enabled:
            return AIResult(ok=False, error="AI disabled (GROQ_ENABLED=false)", error_kind="no_ai")
        if not self.settings.groq_api_key:
            return AIResult(ok=False, error="GROQ_API_KEY is not configured", error_kind="no_ai")

        payload = {
            "model": model or self.model,
            "messages": [{"role": str(m.get("role", "user")),
                          "content": str(m.get("content", ""))} for m in messages],
            "temperature": self.settings.groq_temperature if temperature is None else temperature,
            "max_tokens": self.settings.groq_max_tokens if max_tokens is None else max_tokens,
            "top_p": self.settings.groq_top_p if top_p is None else top_p,
            "stream": False,
        }
        if stop:
            payload["stop"] = list(stop)

        attempts = self.settings.groq_max_retries + 1
        last_error = AIResult(ok=False, error="unknown error", error_kind="api_error")

        for attempt in range(1, attempts + 1):
            started = time.time()
            try:
                response = self._session.post(
                    self.settings.groq_api_url,
                    headers=self.headers(),
                    data=json.dumps(payload),
                    timeout=self.settings.groq_timeout_seconds,
                )
            except requests.exceptions.Timeout:
                last_error = AIResult(ok=False, error="Groq API timeout",
                                      error_kind="timeout", attempts=attempt)
            except requests.exceptions.RequestException as exc:
                last_error = AIResult(ok=False, error=f"network error: {exc}",
                                      error_kind="network", attempts=attempt)
            else:
                latency_ms = int((time.time() - started) * 1000)
                if response.status_code == 200:
                    return self._parse_success(response, latency_ms, payload["model"], attempt)

                kind = ERROR_KIND_BY_STATUS.get(response.status_code, "api_error")
                body = self._safe_body(response)
                last_error = AIResult(
                    ok=False,
                    error=f"HTTP {response.status_code}: {body}",
                    error_kind=kind,
                    status_code=response.status_code,
                    model=payload["model"],
                    latency_ms=latency_ms,
                    attempts=attempt,
                )
                if response.status_code not in RETRYABLE_STATUS:
                    logger.error("Groq request failed (%s): %s", kind, last_error.short_error(200))
                    self._remember_error(last_error)
                    return last_error

            logger.warning("Groq attempt %s/%s failed (%s): %s",
                           attempt, attempts, last_error.error_kind, last_error.short_error(160))
            if attempt < attempts:
                self._sleep_backoff(attempt)

        self._remember_error(last_error)
        return last_error

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _parse_success(self, response: requests.Response, latency_ms: int,
                       model: str, attempt: int) -> AIResult:
        try:
            data = response.json()
        except ValueError:
            return AIResult(ok=False, error="invalid JSON from Groq", error_kind="api_error",
                            status_code=response.status_code, model=model,
                            latency_ms=latency_ms, attempts=attempt)
        try:
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            text = (message.get("content") or "").strip()
            finish_reason = choice.get("finish_reason") or ""
        except Exception as exc:  # pragma: no cover - defensive
            return AIResult(ok=False, error=f"malformed Groq payload: {exc}",
                            error_kind="api_error", model=model, attempts=attempt)

        usage = data.get("usage") or {}
        if not text:
            return AIResult(ok=False, error="Groq returned an empty completion",
                            error_kind="empty", model=model, latency_ms=latency_ms,
                            attempts=attempt, finish_reason=finish_reason)

        with self._lock:
            self._last_status = {"ok": True, "checked_at": time.time(), "model": model,
                                 "error": "", "error_kind": "none"}
            self._last_error = ""

        logger.info("Groq OK model=%s latency=%sms tokens=%s/%s",
                    model, latency_ms, usage.get("prompt_tokens"), usage.get("completion_tokens"))
        return AIResult(
            ok=True,
            text=text,
            model=model,
            latency_ms=latency_ms,
            attempts=attempt,
            finish_reason=finish_reason,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            raw={"id": data.get("id"), "model": data.get("model")},
        )

    @staticmethod
    def _safe_body(response: requests.Response, limit: int = 300) -> str:
        try:
            text = response.text or ""
        except Exception:  # pragma: no cover
            return ""
        return redact(text)[:limit]

    def _remember_error(self, result: AIResult) -> None:
        with self._lock:
            self._last_status = {
                "ok": False,
                "checked_at": time.time(),
                "error": result.short_error(200),
                "error_kind": result.error_kind,
                "status_code": result.status_code,
            }
            self._last_error = result.short_error(200)

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        delay = min(6.0, (0.8 * (2 ** (attempt - 1))) + random.uniform(0, 0.4))
        time.sleep(delay)

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

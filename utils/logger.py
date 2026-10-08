"""Logging setup with automatic secret redaction.

The bot handles credentials (bot token, Groq key, database URL). Those values
must never reach the log stream, not even inside tracebacks. Redaction happens
in two layers:

1. :func:`register_secret` records exact secret strings, which are replaced by
   ``***`` in every formatted record.
2. A regex filter removes anything that *looks* like a credential
   (``postgres://user:pass@host``, ``gsk_...``, ``12345:AA...`` ...).
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any, Iterable

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

REDACTED = "***REDACTED***"

_EXTRA_PATTERNS: list[re.Pattern[str]] = [
    # postgres://user:password@host
    re.compile(r"(?i)\b(postgres(?:ql)?://[^:\s/@]+):([^@\s/]+)@"),
    # secret-looking keys
    re.compile(r"\b(gsk_[A-Za-z0-9_\-]{8,})\b"),
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})\b"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9_\-]{8,})\b"),
    # telegram bot token 123456789:AA...
    re.compile(r"\b(\d{6,}:[A-Za-z0-9_\-]{25,})\b"),
    # api_key=..., "token": "..." inside URLs / headers / JSON
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|bot[_-]?token|auth[_-]?token|token|"
               r"authorization|client[_-]?secret|secret)\b[\"']?\s*[:=]\s*[\"']?([^\s\"'&,}]{6,})"),
]

_REGISTERED: set[str] = set()
_CONFIGURED = False


def register_secret(value: Any) -> None:
    """Remember a literal secret so it is scrubbed from every log record."""
    if value is None:
        return
    text = str(value).strip()
    if len(text) < 6:
        return
    _REGISTERED.add(text)


def register_secrets(values: Iterable[Any]) -> None:
    for value in values:
        register_secret(value)


def clear_registered_secrets() -> None:
    _REGISTERED.clear()


def redact(text: str) -> str:
    """Return ``text`` with every known/possible secret replaced."""
    if not text:
        return text
    out = text
    for pattern in _EXTRA_PATTERNS:
        if pattern.groups >= 2:
            out = pattern.sub(lambda m: f"{m.group(1)}:{REDACTED}@" if "@" in m.group(0)
                              else f"{m.group(1)}={REDACTED}", out)
        else:
            out = pattern.sub(REDACTED, out)
    for secret in _REGISTERED:
        if secret and secret in out:
            out = out.replace(secret, REDACTED)
    return out


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs secrets from the final rendered string."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        rendered = super().format(record)
        return redact(rendered)


class RedactingFilter(logging.Filter):
    """Filter that scrubs secrets from ``record.msg`` before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def configure_logging(level: str = "INFO", *, stream: Any = None) -> None:
    """Configure application-wide logging (idempotent)."""
    global _CONFIGURED
    root = logging.getLogger()
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(numeric)

    if not _CONFIGURED:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(RedactingFormatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
        _CONFIGURED = True
    else:
        for handler in root.handlers:
            handler.setLevel(numeric)

    # Tame noisy third-party loggers
    for name, lvl in {
        "httpx": logging.WARNING,
        "httpcore": logging.WARNING,
        "urllib3": logging.WARNING,
        "telegram.ext.Application": logging.INFO,
        "werkzeug": logging.WARNING,
        "gunicorn.access": logging.INFO,
    }.items():
        logging.getLogger(name).setLevel(lvl)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger; secrets in its output are always redacted."""
    return logging.getLogger(name)


def is_debug() -> bool:
    return os.environ.get("LOG_LEVEL", "INFO").upper() == "DEBUG"

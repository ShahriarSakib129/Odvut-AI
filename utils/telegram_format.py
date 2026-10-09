"""Small, dependency-free formatting helpers for Telegram HTML messages."""

from __future__ import annotations

from html import escape
from typing import Any


def html_text(value: Any, *, limit: int | None = None) -> str:
    """Escape untrusted/dynamic text for Telegram HTML parse mode."""
    text = "" if value is None else str(value)
    if limit is not None:
        text = text[:max(0, int(limit))]
    return escape(text, quote=False)


def html_code(value: Any, *, limit: int | None = None) -> str:
    return f"<code>{html_text(value, limit=limit)}</code>"


def html_bold(value: Any, *, limit: int | None = None) -> str:
    return f"<b>{html_text(value, limit=limit)}</b>"

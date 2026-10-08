"""Telegram package: handlers, message tracking and permissions.

The package intentionally does not import :mod:`telegram.handlers` eagerly.
``handlers.py`` imports the :mod:`services` container, which in turn imports
``telegram.message_tracker`` / ``telegram.permissions`` -- importing handlers
here would create a circular import. Use ``from telegram.handlers import ...``
(that works normally) or the lazy attributes below.
"""

from typing import Any

__all__ = [
    "ALLOWED_UPDATES",
    "Actor",
    "MessageTracker",
    "Permissions",
    "build_application",
    "clean_question",
    "register_handlers",
]


def __getattr__(name: str) -> Any:  # pragma: no cover - thin lazy re-export
    if name in {"build_application", "register_handlers", "ALLOWED_UPDATES"}:
        from . import handlers
        return getattr(handlers, name)
    if name in {"MessageTracker"}:
        from .message_tracker import MessageTracker
        return MessageTracker
    if name in {"Permissions", "Actor", "clean_question"}:
        from . import permissions
        return getattr(permissions, name)
    raise AttributeError(f"module 'telegram' has no attribute {name!r}")

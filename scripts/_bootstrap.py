"""Shared bootstrap for the scripts in this folder.

Adds the project root to ``sys.path``, loads ``.env`` and returns settings in a
uniform way so every script can run as ``python scripts/<name>.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def bootstrap(*, strict: bool = False, quiet: bool = False):
    """Load ``.env`` + environment and return ``(settings, database)``."""
    import config
    from utils.logger import configure_logging

    config.load_env_file_into_environ()
    config.reset_settings_cache()
    settings = config.get_settings()
    configure_logging("WARNING" if quiet else settings.log_level)

    import database

    database.configure(settings)
    return settings, database


def require(settings, keys: list[str]) -> list[str]:
    """Return the list of missing configuration keys."""
    missing = []
    for key in keys:
        value = getattr(settings, key, None)
        if value in (None, "", frozenset(), ()):
            missing.append(key.upper())
    return missing


def print_header(title: str) -> None:
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def print_result(ok: bool, message: str) -> None:
    print(f"[{'OK' if ok else 'FAIL'}] {message}")

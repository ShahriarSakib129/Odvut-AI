"""Shared pytest fixtures.

Environment handling
--------------------
The application reads configuration from the environment at import time, so this
file sets safe *dummy* values **before** any application module is imported.

Database tests run against ``TEST_DATABASE_URL`` (falls back to
``DATABASE_URL``). When no reachable PostgreSQL is available the database tests
are skipped with a clear message instead of failing -- see README > Testing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# --------------------------------------------------------------------------- #
# dummy configuration (never real credentials)
# --------------------------------------------------------------------------- #
os.environ.setdefault("BOT_TOKEN", "123456789:AA-dummy-token-for-tests-only-000000")
os.environ.setdefault("GROQ_API_KEY", "gsk_dummy_key_for_tests_only_0000000000")
os.environ.setdefault("GROQ_MODEL", "openai/gpt-oss-120b")
os.environ.setdefault("TARGET_ADMIN_ID", "777000111")
# 999000444 is a second (bot) admin so tests can prove admin-to-admin chat is
# never stored as member Q&A memory.
os.environ.setdefault("ADMIN_ID", "777000111,999000444")
os.environ.setdefault("GROUP_ID", "-1001234567890")
os.environ.setdefault("PUBLIC_URL", "https://info-group-ai-bot.example.com")
os.environ.setdefault("WEBHOOK_SECRET", "testsecret_testsecret_testsecret_test")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("DROP_PENDING_UPDATES", "true")
os.environ.setdefault("DB_CONNECT_TIMEOUT", "3")
os.environ.setdefault("GROQ_TIMEOUT_SECONDS", "5")
os.environ.setdefault("GROQ_MAX_RETRIES", "0")
# never let the test run connect to Telegram in the background
os.environ["AUTOSTART_BOT"] = "false"

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
if TEST_DB_URL:
    os.environ["DATABASE_URL"] = TEST_DB_URL
else:
    # keep the app importable but "unconfigured" so health/status paths are testable
    os.environ.pop("DATABASE_URL", None)

TEST_CHAT_ID = -1001234567890
TARGET_ADMIN_ID = 777000111
MEMBER_ID = 555000222


def pytest_report_header(config) -> str:  # noqa: ARG001
    database_url = os.environ.get("DATABASE_URL") or "(not set - database tests will skip)"
    return f"info-group-ai-bot tests | DATABASE_URL={database_url}"


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture()
def bot():
    from tests.fakes import FakeBot

    return FakeBot()


@pytest.fixture()
def admin_user():
    from tests.fakes import make_user

    return make_user(TARGET_ADMIN_ID, "target_admin")


@pytest.fixture()
def member_user():
    from tests.fakes import make_user

    return make_user(MEMBER_ID, "member_one")


@pytest.fixture()
def group_chat_id() -> int:
    return TEST_CHAT_ID


@pytest.fixture(scope="session")
def settings():
    import config

    config.reset_settings_cache()
    return config.get_settings()


@pytest.fixture(scope="session")
def db_available(settings) -> bool:
    """Connect once per session; ``False`` disables the database-dependent tests."""
    if not settings.database_url:
        return False
    import database

    database.configure(settings)
    if not database.init():
        return False
    return database.is_available()


@pytest.fixture()
def db(settings, db_available):
    if not db_available:
        pytest.skip("no reachable PostgreSQL (set TEST_DATABASE_URL to run database tests)")
    import database

    database.configure(settings)
    database.init()
    yield database
    database.close_pool()


@pytest.fixture()
def clean_db(db):
    """Empty the memory tables and restore default switches around each test."""
    tables = ("qa_memory", "admin_messages", "pending_questions", "message_logs",
              "conversation_sessions", "response_cache", "ai_usage_log", "bot_users")

    def reset():
        for table in tables:
            db._execute(f"delete from public.{table}")
        for key in ("ai_enabled", "auto_reply_enabled", "memory_enabled",
                    "qa_pairing_enabled", "reply_footer_enabled"):
            db.set_setting(key, "true")
        db.set_memory_generation(0)

    reset()
    yield db
    reset()
    db.close_pool()


@pytest.fixture()
def logger_stream():
    """Capture log output for redaction tests."""
    import io
    import logging

    from utils.logger import RedactingFilter, RedactingFormatter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("redaction-test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield stream, logger
    finally:
        logger.removeHandler(handler)

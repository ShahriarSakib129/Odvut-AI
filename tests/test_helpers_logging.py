"""Runtime helpers + secret redaction tests."""

from __future__ import annotations

import logging
import time

import pytest

from utils.helpers import (
    DedupeTracker,
    RateLimiter,
    TTLCache,
    chunk_text,
    human_count,
    mask_id,
    mask_secret,
)
from utils.logger import redact, register_secret


class TestChunking:
    def test_short_text_single_chunk(self):
        assert chunk_text("hello", 100) == ["hello"]

    def test_long_text_split_on_boundary(self):
        text = ("line one.\n" * 400)
        chunks = chunk_text(text, 200)
        assert len(chunks) > 1
        assert all(len(chunk) <= 200 for chunk in chunks)
        assert "".join(chunks).replace(" ", "") != ""

    def test_empty(self):
        assert chunk_text("") == []


class TestCaches:
    def test_ttl_expiry(self):
        cache = TTLCache(ttl_seconds=0.05, max_entries=10)
        cache.set("k", "v")
        assert cache.get("k") == "v"
        time.sleep(0.08)
        assert cache.get("k") is None

    def test_ttl_size_cap(self):
        cache = TTLCache(ttl_seconds=60, max_entries=3)
        for index in range(6):
            cache.set(index, index)
        assert len(cache) <= 3

    def test_dedupe_tracker(self):
        dedupe = DedupeTracker(60, 100)
        assert dedupe.seen(42) is False
        assert dedupe.seen(42) is True
        assert dedupe.seen(43) is False

    def test_rate_limiter(self):
        limiter = RateLimiter(limit=2, window_seconds=60)
        assert limiter.allow("u1") is True
        assert limiter.allow("u1") is True
        assert limiter.allow("u1") is False
        assert limiter.allow("u2") is True
        assert limiter.remaining("u1") == 0
        assert limiter.retry_after("u1") > 0


class TestFormatting:
    def test_human_count(self):
        assert human_count(999) == "999"
        assert human_count(1500) == "1.5K"
        assert human_count(2_400_000) == "2.4M"
        assert human_count(None) == "0"

    def test_masking(self):
        assert mask_id(123456789).endswith("89")
        assert "***" in mask_id(123456789)
        assert mask_secret("abcdefghijklmno", keep=3).endswith("mno")


class TestRedaction:
    def test_registered_secret_is_removed(self):
        register_secret("SUPERSECRETTOKEN1234567")
        assert "SUPERSECRETTOKEN1234567" not in redact("token=SUPERSECRETTOKEN1234567 ok")

    def test_database_url_password_removed(self):
        cleaned = redact("connecting to postgresql://user:topsecret@db.host:5432/postgres")
        assert "topsecret" not in cleaned
        assert "db.host" in cleaned

    def test_groq_and_telegram_keys_removed(self):
        assert "gsk_abcdefghijklmnop" not in redact("key gsk_abcdefghijklmnop used")
        assert "123456789:AAabcdefghijklmnopqrstuvwxyz" not in redact(
            "bot 123456789:AAabcdefghijklmnopqrstuvwxyz failed")

    def test_logger_output_is_redacted(self, logger_stream):
        stream, logger = logger_stream
        register_secret("MYTELEGRAMBOTTOKEN1234567890")
        logger.info("starting with token MYTELEGRAMBOTTOKEN1234567890 and url "
                    "postgres://u:p4ssw0rd@host/db")
        output = stream.getvalue()
        assert "MYTELEGRAMBOTTOKEN1234567890" not in output
        assert "p4ssw0rd" not in output

    def test_api_key_header_redacted(self):
        assert "abcdef123456" not in redact('{"api_key": "abcdef123456"}')


def test_no_secret_in_settings_summary(settings):
    summary = str(settings.public_summary())
    assert settings.bot_token not in summary
    if settings.groq_api_key:
        assert settings.groq_api_key not in summary


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING"])
def test_configure_logging_levels(level):
    from utils.logger import configure_logging

    configure_logging(level)
    assert logging.getLogger().level == getattr(logging, level)

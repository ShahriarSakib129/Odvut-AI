"""Configuration parsing / validation tests."""

from __future__ import annotations

import pytest

import config
from config import ConfigError, Settings, load_settings, normalize_database_url


def base_env(**overrides) -> dict[str, str]:
    env = {
        "BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
        "GROQ_API_KEY": "gsk_test",
        "DATABASE_URL": "postgresql://user:secret@db.example.com:5432/postgres?pgbouncer=true",
        "TARGET_ADMIN_ID": "123456789",
        "GROUP_ID": "-1001234567890",
        "PUBLIC_URL": "https://example.onrender.com",
        "WEBHOOK_SECRET": "s" * 32,
        "ADMIN_ID": "123456789",
    }
    env.update(overrides)
    return env


def test_valid_environment_loads():
    settings = load_settings(base_env(), strict=True)
    assert settings.is_configured
    assert settings.group_id == -1001234567890
    assert settings.target_admin_ids == frozenset({123456789})
    assert settings.webhook_url == "https://example.onrender.com/webhook"


def test_missing_bot_token_is_reported():
    settings = load_settings(base_env(BOT_TOKEN=""))
    assert not settings.is_configured
    assert any("BOT_TOKEN" in problem for problem in settings.problems)
    with pytest.raises(ConfigError):
        load_settings(base_env(BOT_TOKEN=""), strict=True)


def test_placeholder_values_are_rejected():
    settings = load_settings(base_env(BOT_TOKEN="your_bot_token_here",
                                      DATABASE_URL="your_database_url_here"))
    assert not settings.is_configured
    assert len(settings.problems) >= 2


def test_groq_model_is_configurable():
    settings = load_settings(base_env(GROQ_MODEL="llama-3.3-70b-versatile"))
    assert settings.groq_model == "llama-3.3-70b-versatile"


def test_boolean_and_int_parsing_errors():
    settings = load_settings(base_env(CACHE_ENABLED="maybe"))
    assert any("CACHE_ENABLED" in problem for problem in settings.problems)
    settings = load_settings(base_env(MAX_MEMORY_RESULTS="abc"))
    assert any("MAX_MEMORY_RESULTS" in problem for problem in settings.problems)


def test_bounds_are_enforced():
    settings = load_settings(base_env(GROQ_TEMPERATURE="5.0"))
    assert any("GROQ_TEMPERATURE" in problem for problem in settings.problems)


def test_multiple_target_admins_and_usernames_rejected():
    settings = load_settings(base_env(TARGET_ADMIN_ID="111, 222 ,333"))
    assert settings.target_admin_ids == frozenset({111, 222, 333})
    bad = load_settings(base_env(TARGET_ADMIN_ID="not-a-number"))
    assert not bad.is_configured or any("TARGET_ADMIN_ID" in p for p in bad.problems)


def test_database_url_normalisation():
    url, warnings = normalize_database_url(
        "postgres://user:pw@aws-0.pooler.supabase.com:6543/postgres?pgbouncer=true&sslmode=require"
    )
    assert url.startswith("postgresql://")
    assert "pgbouncer" not in url
    assert "sslmode=require" in url
    assert any("pgbouncer" in w for w in warnings)


def test_database_url_gets_ssl_for_remote_only():
    remote, _ = normalize_database_url("postgresql://u:p@db.example.com/postgres")
    assert "sslmode=require" in remote
    local, _ = normalize_database_url("postgresql://u:p@localhost:5432/postgres")
    assert "sslmode" not in local


def test_password_is_redacted_in_summary():
    settings = load_settings(base_env())
    summary = settings.public_summary()
    rendered = str(summary)
    assert ":secret@" not in rendered, "the raw password must never be summarised"
    assert settings.database_url not in rendered
    assert rendered.count("secret") == rendered.count("webhook_secret_set")
    assert settings.redacted_database_url.startswith("postgresql://user:***@")


def test_webhook_secret_required_when_public_url_set():
    settings = load_settings(base_env(WEBHOOK_SECRET=""))
    assert not settings.is_configured
    assert any("WEBHOOK_SECRET" in problem for problem in settings.problems)
    relaxed = load_settings(base_env(WEBHOOK_SECRET="", WEBHOOK_REQUIRE_SECRET="false"))
    assert relaxed.is_configured


def test_trigger_keywords_parsing():
    settings = load_settings(base_env(TRIGGER_KEYWORDS="ai, bot; admin"))
    assert settings.trigger_keywords == ("ai", "bot", "admin")
    assert settings.autostart_bot is True


def test_cached_settings_singleton():
    config.reset_settings_cache()
    first = config.get_settings(load_env_file=False)
    second = config.get_settings(load_env_file=False)
    assert first is second
    config.reset_settings_cache()
    assert config.get_settings(load_env_file=False) is not first


def test_secret_map_never_empty_for_tokens():
    settings = load_settings(base_env())
    secrets = settings.secret_map()
    assert secrets["BOT_TOKEN"].startswith("123456789:")
    assert secrets["DATABASE_PASSWORD"] == "secret"


def test_defaults_match_documented_values():
    settings = Settings()
    assert settings.groq_model == "openai/gpt-oss-120b"
    assert settings.max_memory_results == 10
    assert settings.max_qa_results == 5
    assert settings.max_context_chars == 12000
    assert settings.webhook_path == "/webhook"


def test_database_url_must_look_like_postgres():
    settings = load_settings(base_env(DATABASE_URL="mysql://user:pass@host/db"))
    assert not settings.is_configured
    assert any("postgresql://" in problem for problem in settings.problems)


def test_autostart_can_be_disabled():
    settings = load_settings(base_env(AUTOSTART_BOT="false"))
    assert settings.autostart_bot is False

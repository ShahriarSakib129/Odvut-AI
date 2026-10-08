"""Central configuration for INFO GROUP AI BOT.

Every value that may change between deployments is read from environment
variables (``.env`` in development, Render/Heroku config vars in production).

Nothing secret is ever hard-coded in this project. If a value looks like a
credential it *must* come from the environment.

Public API
----------
``get_settings()``   -> cached :class:`Settings` instance (validated)
``load_settings()``  -> build a fresh :class:`Settings` from a mapping
``ConfigError``      -> raised when validation fails
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:  # python-dotenv is optional at runtime (Render injects real env vars)
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - only when the dependency is missing
    load_dotenv = None  # type: ignore[assignment]

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
SCHEMA_FILE = PROJECT_ROOT / "database" / "schema.sql"

DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"

#: libpq connection parameters that psycopg2 understands. Anything else in a
#: connection URL (``pgbouncer``, ``supavisor``, ...) is dropped because
#: psycopg2 would raise ``invalid connection option``.
KNOWN_DB_PARAMS = {
    "sslmode",
    "sslcert",
    "sslkey",
    "sslrootcert",
    "sslcrl",
    "application_name",
    "connect_timeout",
    "options",
    "channel_binding",
    "target_session_attrs",
    "keepalives",
    "keepalives_idle",
    "keepalives_interval",
    "keepalives_count",
    "gssencmode",
}

_TRUE = {"1", "true", "t", "yes", "y", "on", "enable", "enabled"}
_FALSE = {"0", "false", "f", "no", "n", "off", "disable", "disabled", "none", "null", ""}

_TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_\-]{20,}$")
_UNSET_MARKERS = {
    "",
    "your_bot_token_here",
    "replace_me",
    "changeme",
    "todo",
    "none",
    "null",
    "xxxxx",
}


class ConfigError(RuntimeError):
    """Raised when the configuration is invalid or incomplete."""

    def __init__(self, problems: Sequence[str]):
        self.problems = list(problems)
        super().__init__("Configuration problem(s): " + "; ".join(self.problems))


# --------------------------------------------------------------------------- #
# primitive readers
# --------------------------------------------------------------------------- #
def env_str(env: Mapping[str, str], key: str, default: str = "") -> str:
    raw = env.get(key)
    if raw is None:
        return default
    return raw.strip()


def env_bool(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError([f"{key} must be a boolean (true/false), got: {raw!r}"])


def env_int(env: Mapping[str, str], key: str, default: int, minimum: int | None = None,
            maximum: int | None = None) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        value = default
    else:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ConfigError([f"{key} must be an integer, got: {raw!r}"]) from None
    if minimum is not None and value < minimum:
        raise ConfigError([f"{key} must be >= {minimum}, got: {value}"])
    if maximum is not None and value > maximum:
        raise ConfigError([f"{key} must be <= {maximum}, got: {value}"])
    return value


def env_float(env: Mapping[str, str], key: str, default: float, minimum: float | None = None,
              maximum: float | None = None) -> float:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        value = default
    else:
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            raise ConfigError([f"{key} must be a number, got: {raw!r}"]) from None
    if minimum is not None and value < minimum:
        raise ConfigError([f"{key} must be >= {minimum}, got: {value}"])
    if maximum is not None and value > maximum:
        raise ConfigError([f"{key} must be <= {maximum}, got: {value}"])
    return value


def env_list(env: Mapping[str, str], key: str, default: Sequence[str] = ()) -> tuple[str, ...]:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return tuple(default)
    parts = [p.strip() for p in re.split(r"[,\n;]+", raw) if p.strip()]
    return tuple(parts)


def env_ids(env: Mapping[str, str], key: str) -> frozenset[int]:
    """Parse ``123, 456`` (also accepts ``@username`` entries) into a set of ints."""
    ids: set[int] = set()
    for part in env_list(env, key):
        part = part.strip()
        if part.startswith("@"):
            continue  # usernames are resolved at runtime instead
        if not re.fullmatch(r"-?\d+", part):
            raise ConfigError([f"{key} entry must be a numeric Telegram id, got: {part!r}"])
        ids.add(int(part))
    return frozenset(ids)


def is_placeholder(value: str) -> bool:
    lowered = (value or "").strip().strip('"').strip("'").lower()
    if lowered in _UNSET_MARKERS:
        return True
    return lowered.startswith(("your_", "<", "paste_", "xxx"))


# --------------------------------------------------------------------------- #
# database url handling
# --------------------------------------------------------------------------- #
def normalize_database_url(url: str, *, force_ssl: bool = True) -> tuple[str, list[str]]:
    """Return ``(normalized_url, warnings)``.

    * ``postgres://`` is rewritten to ``postgresql://`` (Supabase/Render format).
    * unknown query parameters such as ``pgbouncer=true`` are dropped, because
      psycopg2 raises ``invalid connection option`` for them.
    * ``sslmode=require`` is added for remote hosts unless already present.
    """
    warnings: list[str] = []
    url = (url or "").strip()
    if not url:
        return "", warnings
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    parts = urlsplit(url)
    try:
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
    except Exception:
        query = {}

    dropped = sorted(k for k in query if k not in KNOWN_DB_PARAMS)
    if dropped:
        warnings.append(
            "Dropped unsupported DATABASE_URL parameter(s): " + ", ".join(dropped)
        )
        for key in dropped:
            query.pop(key, None)

    host = (parts.hostname or "").lower()
    is_local = host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".local")
    if force_ssl and not is_local and "sslmode" not in query:
        query["sslmode"] = "require"
        warnings.append("DATABASE_URL: sslmode=require added automatically")

    new_query = urlencode(query)
    normalized = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    return normalized, warnings


def redact_database_url(url: str) -> str:
    """Mask the password inside a connection URL so it is safe to log."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except Exception:
        return "***"
    if not parts.password:
        return url
    netloc = parts.netloc.replace(f"{parts.password}@", "***@")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# --------------------------------------------------------------------------- #
# settings object
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Settings:
    """Immutable, validated application settings."""

    # --- core -----------------------------------------------------------
    bot_name: str = "INFO GROUP AI BOT"
    bot_token: str = ""
    environment: str = "production"

    # --- telegram -------------------------------------------------------
    group_id: int | None = None
    target_admin_ids: frozenset[int] = frozenset()
    target_admin_username: str = ""
    admin_ids: frozenset[int] = frozenset()
    allow_group_admins: bool = False

    # --- webhook / flask ------------------------------------------------
    public_url: str = ""
    webhook_secret: str = ""
    webhook_path: str = "/webhook"
    set_webhook_on_startup: bool = True
    webhook_require_secret: bool = True
    drop_pending_updates: bool = True
    webhook_max_connections: int = 20

    # --- database -------------------------------------------------------
    database_url: str = ""
    db_pool_min: int = 0
    db_pool_max: int = 4
    db_connect_timeout: int = 10
    db_statement_timeout_ms: int = 15000
    db_auto_migrate: bool = False

    # --- groq / ai ------------------------------------------------------
    groq_api_key: str = ""
    groq_model: str = DEFAULT_GROQ_MODEL
    groq_api_url: str = DEFAULT_GROQ_API_URL
    groq_models_url: str = DEFAULT_GROQ_MODELS_URL
    groq_timeout_seconds: int = 30
    groq_max_retries: int = 2
    groq_temperature: float = 0.35
    groq_top_p: float = 0.95
    groq_max_tokens: int = 800
    groq_enabled: bool = True
    ai_answer_without_memory: bool = True
    ai_disclosure_in_group: bool = True
    ai_disclosure_text: str = "🤖 AI উত্তর (Admin-এর শেয়ার করা তথ্য ও স্টাইল অনুসারে)"

    # --- retrieval ------------------------------------------------------
    max_memory_results: int = 10
    max_qa_results: int = 5
    max_context_chars: int = 12000
    candidate_limit: int = 200
    min_relevance_score: float = 0.06
    recency_half_life_days: float = 45.0
    qa_priority_boost: float = 1.25
    dedupe_threshold: float = 0.86
    include_recent_fallback: int = 8
    embedding_enabled: bool = True
    embedding_dim: int = 512

    # --- q&a pairing ----------------------------------------------------
    qa_pair_window_seconds: int = 300
    qa_pair_require_question: bool = False
    qa_pair_lone_question: bool = False
    pending_question_ttl_seconds: int = 1800
    track_all_member_messages: bool = False
    log_unanswered_questions: bool = True
    log_text_chars: int = 600

    # --- behaviour ------------------------------------------------------
    trigger_on_mention: bool = True
    trigger_on_reply: bool = True
    trigger_on_command: bool = True
    trigger_keywords: tuple[str, ...] = ()
    answer_in_private_chat: bool = True
    rate_limit_per_minute: int = 6
    max_question_chars: int = 1200
    max_answer_chars: int = 4000
    typing_indicator: bool = True

    # --- process lifecycle ----------------------------------------------
    #: start the Telegram application in the background as soon as the Flask
    #: app is imported (disable in tests / offline tooling)
    autostart_bot: bool = True

    # --- cache ----------------------------------------------------------
    cache_enabled: bool = True
    cache_ttl_seconds: int = 900
    cache_max_entries: int = 500

    # --- logging --------------------------------------------------------
    log_level: str = "INFO"
    log_requests: bool = True
    maintenance_interval_seconds: int = 21600

    #: non-fatal problems collected while parsing the environment
    warnings: tuple[str, ...] = field(default_factory=tuple)
    #: fatal problems; when non-empty the application refuses to serve webhooks
    problems: tuple[str, ...] = field(default_factory=tuple)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @property
    def is_configured(self) -> bool:
        return not self.problems

    @property
    def webhook_url(self) -> str:
        if not self.public_url:
            return ""
        return self.public_url.rstrip("/") + self.webhook_path

    @property
    def redacted_database_url(self) -> str:
        return redact_database_url(self.database_url)

    def is_target_admin(self, user_id: int | None) -> bool:
        return user_id is not None and int(user_id) in self.target_admin_ids

    def is_bot_admin(self, user_id: int | None) -> bool:
        return user_id is not None and int(user_id) in self.admin_ids

    def secret_map(self) -> dict[str, str]:
        """Secrets that must never appear in logs."""
        return {
            "BOT_TOKEN": self.bot_token,
            "GROQ_API_KEY": self.groq_api_key,
            "WEBHOOK_SECRET": self.webhook_secret,
            "DATABASE_URL": self.database_url,
            "DATABASE_PASSWORD": () if not self.database_url else self._db_password(),
        }

    def _db_password(self) -> str:
        try:
            return urlsplit(self.database_url).password or ""
        except Exception:
            return ""

    def public_summary(self) -> dict[str, Any]:
        """Safe-to-log summary (no secrets)."""
        return {
            "bot_name": self.bot_name,
            "environment": self.environment,
            "groq_model": self.groq_model,
            "groq_enabled": self.groq_enabled,
            "group_id_set": self.group_id is not None,
            "target_admins": sorted(self.target_admin_ids),
            "target_admin_username": self.target_admin_username or None,
            "bot_admins": sorted(self.admin_ids),
            "public_url": self.public_url or None,
            "webhook_secret_set": bool(self.webhook_secret),
            "database_configured": bool(self.database_url),
            "database_url": self.redacted_database_url or None,
            "max_memory_results": self.max_memory_results,
            "max_qa_results": self.max_qa_results,
            "max_context_chars": self.max_context_chars,
            "cache_enabled": self.cache_enabled,
            "embedding_enabled": self.embedding_enabled,
            "warnings": list(self.warnings),
            "problems": list(self.problems),
        }


def load_settings(env: Mapping[str, str] | None = None, *, strict: bool = False) -> Settings:
    """Build a :class:`Settings` object from ``env`` (defaults to ``os.environ``).

    ``strict=True`` raises :class:`ConfigError` when required values are missing.
    """
    env = dict(os.environ if env is None else env)
    warnings: list[str] = []
    problems: list[str] = []

    bot_token = env_str(env, "BOT_TOKEN") or env_str(env, "TELEGRAM_BOT_TOKEN")
    if not bot_token or is_placeholder(bot_token):
        problems.append("BOT_TOKEN is missing (get it from @BotFather)")
    elif not _TELEGRAM_TOKEN_RE.match(bot_token):
        problems.append("BOT_TOKEN format looks invalid (expected '123456:AA...')")

    groq_api_key = env_str(env, "GROQ_API_KEY")
    if not groq_api_key or is_placeholder(groq_api_key):
        warnings.append("GROQ_API_KEY is missing (AI answers will be disabled)")

    raw_database_url = env_str(env, "DATABASE_URL")
    database_url, url_warnings = normalize_database_url(raw_database_url)
    warnings.extend(url_warnings)
    if not database_url or is_placeholder(raw_database_url):
        problems.append("DATABASE_URL is missing (Supabase PostgreSQL connection string)")
    elif not database_url.startswith(("postgresql://", "postgres://")):
        problems.append(
            "DATABASE_URL must be a PostgreSQL connection string starting with "
            "'postgresql://' (copy the URI from Supabase -> Project Settings -> Database)"
        )

    group_id_raw = env_str(env, "GROUP_ID")
    group_id: int | None = None
    if group_id_raw:
        if re.fullmatch(r"-?\d+", group_id_raw):
            group_id = int(group_id_raw)
        else:
            problems.append(f"GROUP_ID must be numeric (e.g. -1001234567890), got {group_id_raw!r}")

    try:
        target_admin_ids = env_ids(env, "TARGET_ADMIN_ID")
    except ConfigError as exc:
        target_admin_ids = frozenset()
        problems.extend(exc.problems)

    try:
        admin_ids = env_ids(env, "ADMIN_ID") | env_ids(env, "ADMIN_IDS")
    except ConfigError as exc:
        admin_ids = frozenset()
        problems.extend(exc.problems)

    target_admin_username = (env_str(env, "TARGET_ADMIN_USERNAME") or "").lstrip("@").strip()
    if not target_admin_ids and not target_admin_username:
        warnings.append(
            "Neither TARGET_ADMIN_ID nor TARGET_ADMIN_USERNAME is set: no admin memory "
            "will be collected. Send /whoami in the group to learn your Telegram id."
        )

    if admin_ids and target_admin_ids and not (admin_ids & target_admin_ids):
        warnings.append(
            "ADMIN_ID and TARGET_ADMIN_ID are different users. ADMIN_ID controls the admin "
            "commands, TARGET_ADMIN_ID controls whose messages become memory."
        )

    public_url = env_str(env, "PUBLIC_URL").rstrip("/")
    if public_url and not public_url.startswith(("http://", "https://")):
        problems.append("PUBLIC_URL must start with http:// or https:// (Render URL)")
    if public_url and public_url.startswith("http://") and env_str(env, "ENVIRONMENT", "production") == "production":
        warnings.append("PUBLIC_URL uses plain http://; Telegram webhooks require https://")

    webhook_secret = env_str(env, "WEBHOOK_SECRET")
    webhook_require_secret = env_bool(env, "WEBHOOK_REQUIRE_SECRET", True)
    if webhook_require_secret and not webhook_secret and public_url:
        problems.append(
            "WEBHOOK_SECRET is empty but WEBHOOK_REQUIRE_SECRET=true. Generate one with "
            "'python -c \"import secrets;print(secrets.token_urlsafe(32))\"'"
        )
    if webhook_secret and len(webhook_secret) < 16:
        warnings.append("WEBHOOK_SECRET is shorter than 16 characters; use a longer random value")
    if not public_url:
        warnings.append(
            "PUBLIC_URL is not set: the bot will not register a Telegram webhook automatically."
        )

    embedding_dim = env_int(env, "EMBEDDING_DIM", 512, minimum=64, maximum=4096)

    try:
        settings = Settings(
            bot_name=env_str(env, "BOT_NAME", "INFO GROUP AI BOT"),
            bot_token=bot_token,
            environment=env_str(env, "ENVIRONMENT", "production").lower(),
            group_id=group_id,
            target_admin_ids=target_admin_ids,
            target_admin_username=target_admin_username,
            admin_ids=admin_ids,
            allow_group_admins=env_bool(env, "ALLOW_GROUP_ADMINS", False),
            public_url=public_url,
            webhook_secret=webhook_secret,
            webhook_path=_normalize_path(env_str(env, "WEBHOOK_PATH", "/webhook")),
            set_webhook_on_startup=env_bool(env, "SET_WEBHOOK_ON_STARTUP", True),
            webhook_require_secret=webhook_require_secret,
            drop_pending_updates=env_bool(env, "DROP_PENDING_UPDATES", True),
            webhook_max_connections=env_int(env, "WEBHOOK_MAX_CONNECTIONS", 20, minimum=1, maximum=100),
            database_url=database_url,
            db_pool_min=env_int(env, "DB_POOL_MIN", 0, minimum=0, maximum=20),
            db_pool_max=env_int(env, "DB_POOL_MAX", 4, minimum=1, maximum=50),
            db_connect_timeout=env_int(env, "DB_CONNECT_TIMEOUT", 10, minimum=1, maximum=60),
            db_statement_timeout_ms=env_int(env, "DB_STATEMENT_TIMEOUT_MS", 15000, minimum=1000),
            db_auto_migrate=env_bool(env, "DB_AUTO_MIGRATE", False),
            groq_api_key=groq_api_key,
            groq_model=env_str(env, "GROQ_MODEL", DEFAULT_GROQ_MODEL),
            groq_api_url=env_str(env, "GROQ_API_URL", DEFAULT_GROQ_API_URL),
            groq_models_url=env_str(env, "GROQ_MODELS_URL", DEFAULT_GROQ_MODELS_URL),
            groq_timeout_seconds=env_int(env, "GROQ_TIMEOUT_SECONDS", 30, minimum=5, maximum=120),
            groq_max_retries=env_int(env, "GROQ_MAX_RETRIES", 2, minimum=0, maximum=5),
            groq_temperature=env_float(env, "GROQ_TEMPERATURE", 0.35, minimum=0.0, maximum=2.0),
            groq_top_p=env_float(env, "GROQ_TOP_P", 0.95, minimum=0.0, maximum=1.0),
            groq_max_tokens=env_int(env, "GROQ_MAX_TOKENS", 800, minimum=64, maximum=8192),
            groq_enabled=env_bool(env, "GROQ_ENABLED", True),
            ai_answer_without_memory=env_bool(env, "AI_ANSWER_WITHOUT_MEMORY", True),
            ai_disclosure_in_group=env_bool(env, "AI_DISCLOSURE_IN_GROUP", True),
            ai_disclosure_text=env_str(env, "AI_DISCLOSURE_TEXT", Settings.ai_disclosure_text),
            max_memory_results=env_int(env, "MAX_MEMORY_RESULTS", 10, minimum=0, maximum=50),
            max_qa_results=env_int(env, "MAX_QA_RESULTS", 5, minimum=0, maximum=30),
            max_context_chars=env_int(env, "MAX_CONTEXT_CHARS", 12000, minimum=1000, maximum=60000),
            candidate_limit=env_int(env, "MEMORY_CANDIDATE_LIMIT", 200, minimum=20, maximum=2000),
            min_relevance_score=env_float(env, "MIN_RELEVANCE_SCORE", 0.06, minimum=0.0, maximum=1.0),
            recency_half_life_days=env_float(env, "RECENCY_HALF_LIFE_DAYS", 45.0, minimum=1.0),
            qa_priority_boost=env_float(env, "QA_PRIORITY_BOOST", 1.25, minimum=1.0, maximum=3.0),
            dedupe_threshold=env_float(env, "DEDUPE_THRESHOLD", 0.86, minimum=0.5, maximum=1.0),
            include_recent_fallback=env_int(env, "INCLUDE_RECENT_FALLBACK", 8, minimum=0, maximum=50),
            embedding_enabled=env_bool(env, "EMBEDDING_ENABLED", True),
            embedding_dim=embedding_dim,
            qa_pair_window_seconds=env_int(env, "QA_PAIR_WINDOW_SECONDS", 300, minimum=30, maximum=86400),
            qa_pair_require_question=env_bool(env, "QA_PAIR_REQUIRE_QUESTION", False),
            qa_pair_lone_question=env_bool(env, "QA_PAIR_LONE_QUESTION", False),
            pending_question_ttl_seconds=env_int(env, "PENDING_QUESTION_TTL_SECONDS", 1800,
                                                 minimum=60, maximum=86400),
            track_all_member_messages=env_bool(env, "TRACK_ALL_MEMBER_MESSAGES", False),
            log_unanswered_questions=env_bool(env, "LOG_UNANSWERED_QUESTIONS", True),
            log_text_chars=env_int(env, "LOG_TEXT_CHARS", 600, minimum=0, maximum=10000),
            trigger_on_mention=env_bool(env, "TRIGGER_ON_MENTION", True),
            trigger_on_reply=env_bool(env, "TRIGGER_ON_REPLY", True),
            trigger_on_command=env_bool(env, "TRIGGER_ON_COMMAND", True),
            trigger_keywords=env_list(env, "TRIGGER_KEYWORDS"),
            answer_in_private_chat=env_bool(env, "ANSWER_IN_PRIVATE_CHAT", True),
            rate_limit_per_minute=env_int(env, "RATE_LIMIT_PER_MINUTE", 6, minimum=1, maximum=120),
            max_question_chars=env_int(env, "MAX_QUESTION_CHARS", 1200, minimum=100, maximum=4000),
            max_answer_chars=env_int(env, "MAX_ANSWER_CHARS", 4000, minimum=200, maximum=4096),
            typing_indicator=env_bool(env, "TYPING_INDICATOR", True),
            autostart_bot=env_bool(env, "AUTOSTART_BOT", True),
            cache_enabled=env_bool(env, "CACHE_ENABLED", True),
            cache_ttl_seconds=env_int(env, "CACHE_TTL_SECONDS", 900, minimum=0, maximum=86400),
            cache_max_entries=env_int(env, "CACHE_MAX_ENTRIES", 500, minimum=10, maximum=10000),
            log_level=env_str(env, "LOG_LEVEL", "INFO").upper(),
            log_requests=env_bool(env, "LOG_REQUESTS", True),
            maintenance_interval_seconds=env_int(env, "MAINTENANCE_INTERVAL_SECONDS", 21600,
                                                 minimum=300, maximum=604800),
            warnings=tuple(warnings),
            problems=tuple(problems),
        )
    except ConfigError as exc:  # pragma: no cover - aggregated above in most cases
        problems.extend(exc.problems)
        settings = Settings(warnings=tuple(warnings), problems=tuple(problems))

    if strict and settings.problems:
        raise ConfigError(settings.problems)
    return settings


def _normalize_path(raw: str) -> str:
    path = (raw or "/webhook").strip()
    if not path.startswith("/"):
        path = "/" + path
    return path


_CACHED: Settings | None = None


def get_settings(*, reload: bool = False, strict: bool = False,
                 load_env_file: bool = True) -> Settings:
    """Return a cached :class:`Settings` instance."""
    global _CACHED
    if _CACHED is None or reload:
        if load_env_file:
            load_env_file_into_environ()
        _CACHED = load_settings(strict=strict)
    return _CACHED


def load_env_file_into_environ(path: Path = ENV_FILE) -> bool:
    """Load ``.env`` if present. Existing environment variables win."""
    if load_dotenv is None or not Path(path).exists():
        return False
    load_dotenv(dotenv_path=str(path), override=False)
    return True


def reset_settings_cache() -> None:
    global _CACHED
    _CACHED = None


def iter_config_keys() -> Iterable[str]:
    """Environment variable names this project reads (for docs / validation)."""
    return (
        "BOT_TOKEN", "GROQ_API_KEY", "GROQ_MODEL", "GROQ_API_URL", "DATABASE_URL",
        "TARGET_ADMIN_ID", "TARGET_ADMIN_USERNAME", "GROUP_ID", "PUBLIC_URL",
        "WEBHOOK_SECRET", "WEBHOOK_PATH", "ADMIN_ID", "ADMIN_IDS",
    )

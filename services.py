"""Service container: one place where every subsystem is wired together.

``build_services(settings)`` is called once per process (by ``bot.py`` for the
Telegram application and by ``app.py`` for the Flask webhooks). Keeping the
wiring here means handlers never construct objects themselves, and tests can
inject fakes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import database
from ai.caching import AnswerCache
from ai.groq_client import GroqClient
from ai.responder import AnswerService
from ai.retrieval import RetrievalEngine
from config import Settings, get_settings
from bot_telegram.message_tracker import MessageTracker
from bot_telegram.permissions import Permissions
from utils.helpers import DedupeTracker, RateLimiter, TTLCache, Throttle
from utils.logger import get_logger, register_secrets

logger = get_logger(__name__)


@dataclass
class Services:
    """All long-lived objects a handler may need."""

    settings: Settings
    db: Any = database
    permissions: Permissions | None = None
    tracker: MessageTracker | None = None
    retriever: RetrievalEngine | None = None
    groq: GroqClient | None = None
    cache: AnswerCache | None = None
    responder: AnswerService | None = None
    dedupe: DedupeTracker = field(default_factory=lambda: DedupeTracker(900, 4000))
    rate_limiter: RateLimiter | None = None
    settings_cache: TTLCache = field(default_factory=lambda: TTLCache(60, 64))
    maintenance: Throttle | None = None
    ready: bool = False
    errors: list[str] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)

    # ------------------------------------------------------------------ #
    def runtime_flag(self, key: str, default: bool = True) -> bool:
        """Read a ``bot_settings`` flag with a 60s cache (falls back to default)."""
        cached = self.settings_cache.get(key)
        if cached is not None:
            return bool(cached)
        value: Any = None
        try:
            value = self.db.get_setting(key)
        except Exception:
            value = None
        if value is None:
            self.settings_cache.set(key, default)
            return default
        normalized = str(value).strip().lower() in {"1", "true", "t", "yes", "y", "on", "enabled"}
        self.settings_cache.set(key, normalized)
        return normalized

    def set_runtime_flag(self, key: str, value: bool, *, updated_by: int | None = None) -> bool:
        ok = self.db.set_setting(key, "true" if value else "false",
                                 updated_by=updated_by, value_type="bool")
        if ok:
            self.settings_cache.set(key, bool(value))
        return ok

    def db_ready(self) -> bool:
        try:
            return bool(database.is_available())
        except Exception:
            return False

    def health(self) -> dict[str, Any]:
        """Aggregated health used by ``/status`` and ``/health``."""
        with self.lock:
            return {
                "ready": self.ready,
                "bot": self.settings.bot_name,
                "environment": self.settings.environment,
                "database": database.health(),
                "db_pool": database.stats(),
                "ai": self.responder.health() if self.responder else {"enabled": False},
                "memory_generation": database.memory_generation,
                "errors": list(self.errors),
            }


_services: Services | None = None
_services_lock = threading.RLock()


def build_services(settings: Settings | None = None, *, force: bool = False) -> Services:
    """Create (or return) the process-wide :class:`Services` instance."""
    global _services
    with _services_lock:
        if _services is not None and not force:
            return _services
        settings = settings or get_settings()
        register_secrets(settings.secret_map().values())
        database.configure(settings)
        # Do not probe PostgreSQL while constructing the Telegram application.
        # A slow or temporarily blocked Supabase connection must not leave the
        # webhook manager in ``starting`` forever.  startup_tasks() retries the
        # same initialization after the Telegram application is serving updates.
        db_available = False

        permissions = Permissions(settings)
        groq = GroqClient(settings)
        cache = AnswerCache(settings, database)
        retriever = RetrievalEngine(database, settings)
        tracker = MessageTracker(database, settings, cache=cache)
        responder = AnswerService(settings, database, retriever, groq, cache)

        services = Services(
            settings=settings,
            db=database,
            permissions=permissions,
            tracker=tracker,
            retriever=retriever,
            groq=groq,
            cache=cache,
            responder=responder,
            rate_limiter=RateLimiter(settings.rate_limit_per_minute, 60.0),
            maintenance=Throttle(settings.maintenance_interval_seconds),
            ready=db_available,
        )
        errors: list[str] = []
        if not db_available:
            errors.append("database unavailable")
        if not groq.enabled:
            errors.append("AI disabled (GROQ_API_KEY missing)")
        services.errors = errors
        logger.info("services built: ready=%s errors=%s", services.ready, errors or "none")
        _services = services
        return services


def get_services() -> Services:
    return build_services()


def reset_services() -> None:
    global _services
    with _services_lock:
        _services = None


# --------------------------------------------------------------------------- #
# background maintenance
# --------------------------------------------------------------------------- #
def run_maintenance(services: Services, *, force: bool = False) -> dict[str, int] | None:
    """Purge expired questions/cache/logs. Throttled to avoid extra DB load."""
    if services.maintenance is None:
        return None
    if not force and not services.maintenance.ready():
        return None
    try:
        return database.run_maintenance()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("maintenance run failed: %s", exc)
        return None


def startup_tasks(services: Services) -> dict[str, Any]:
    """Work to do once the process is alive (called from bot/app startup)."""
    summary: dict[str, Any] = {}
    try:
        if not services.ready:
            summary["db_retry"] = database.init(
                apply_schema_if_missing=services.settings.db_auto_migrate
            )
        expire = database.expire_pending_questions()
        summary["expired_pending_questions"] = expire
        run_maintenance(services, force=True)
        summary["memory_generation"] = database.memory_generation
        services.ready = database.is_available()
    except Exception as exc:  # pragma: no cover
        logger.warning("startup tasks failed: %s", exc)
        summary["error"] = str(exc)[:200]
    return summary

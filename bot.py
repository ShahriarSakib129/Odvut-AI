"""Telegram application lifecycle for webhook (production) and polling (local).

Render's free tier cannot keep a long-polling worker alive, so production uses
**webhooks**: Flask receives the update, and this module feeds it into a
``python-telegram-bot`` ``Application`` that runs in a dedicated background
thread with its own asyncio event loop (PTB v20+ has no built-in webhook
server -- this is the officially recommended way to drive it yourself).

Local development can use either mode::

    python bot.py            # long polling (no PUBLIC_URL needed)
    python app.py            # Flask + webhook (use ngrok/cloudflared locally)

Why a background thread instead of ``asyncio.run`` per request:

* handlers stay warm (no re-connect to Telegram on every update)
* the event loop keeps the HTTP connection pool, job queue and caches alive
* it plays nicely with gunicorn's sync workers (default 1 worker)
"""

from __future__ import annotations

import asyncio
import atexit
import threading
import time
from typing import Any

import requests
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application

from config import Settings, get_settings
from bot_telegram.handlers import ALLOWED_UPDATES, _post_init, build_application
from utils.logger import configure_logging, get_logger, redact, register_secrets

logger = get_logger(__name__)


#: minimum seconds between two start attempts (a bad token must not hammer
#: the Telegram API when /health or /webhook is polled)
START_COOLDOWN_SECONDS = 20.0


class ApplicationNotReady(RuntimeError):
    """Raised when an update arrives before the application finished starting."""



WEBHOOK_SETUP_TIMEOUT_SECONDS = 60.0
START_STEP_TIMEOUT = 45.0
WEBHOOK_START_TIMEOUT_SECONDS = 60.0


def schedule_webhook_setup(loop: asyncio.AbstractEventLoop, application: Any) -> None:
    """Run ``_post_init`` (webhook registration + command menu) without blocking start.

    Non-blocking by design: it returns immediately, the work runs as a task on
    ``loop``, is bounded by a timeout, and any failure is logged (never raised).
    """

    async def _runner() -> None:
        try:
            await asyncio.wait_for(_post_init(application),
                                   timeout=WEBHOOK_SETUP_TIMEOUT_SECONDS)
            logger.info("webhook setup finished")
        except asyncio.TimeoutError:
            logger.error("webhook setup timed out after %.0fs (will retry on next restart "
                         "or /set_webhook)", WEBHOOK_SETUP_TIMEOUT_SECONDS)
        except Exception as exc:  # never let a setup hiccup affect the running bot
            logger.error("webhook setup failed: %s", redact(str(exc))[:200])

    loop.create_task(_runner())


class ApplicationManager:
    """Owns the PTB ``Application`` and its background event loop."""

    def __init__(self, settings: Settings | None = None, *, mode: str = "webhook"):
        self.settings = settings or get_settings()
        self.mode = mode
        self._app: Application | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._started = False
        self._starting = False
        self._start_error = ""
        self._last_update_at: float = 0.0
        self._update_count = 0
        self._failed_updates = 0
        self._started_at: float = 0.0
        self._attempts: int = 0
        self._last_attempt_at: float = 0.0
        register_secrets(self.settings.secret_map().values())

    # ------------------------------------------------------------------ #
    # properties
    # ------------------------------------------------------------------ #
    @property
    def application(self) -> Application | None:
        return self._app

    @property
    def started(self) -> bool:
        return self._started

    @property
    def starting(self) -> bool:
        return self._starting

    @property
    def start_error(self) -> str:
        return self._start_error

    @property
    def bot_username(self) -> str | None:
        if self._app is None:
            return None
        return self._app.bot_data.get("bot_username")

    def stats(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "started": self._started,
            "starting": self._starting,
            "start_error": self._start_error,
            "updates_received": self._update_count,
            "updates_failed": self._failed_updates,
            "last_update_at": self._last_update_at or None,
            "uptime_seconds": int(time.time() - self._started_at) if self._started_at else 0,
            "start_attempts": self._attempts,
            "retry_in_seconds": self.retry_in_seconds,
            "bot_username": self.bot_username,
        }

    # ------------------------------------------------------------------ #
    # startup / shutdown
    # ------------------------------------------------------------------ #
    def start(self, *, wait: bool = True, timeout: float = 30.0) -> bool:
        """Start the application once. Safe to call from many threads."""
        with self._lock:
            if self._started:
                return True
            if self._starting:
                if not wait:
                    return False
                deadline = time.time() + timeout
                while self._starting and time.time() < deadline:
                    time.sleep(0.2)
                return self._started
            self._starting = True
            self._attempts += 1
            self._last_attempt_at = time.time()
            self._start_error = ""   # a retry must not inherit the old error

        thread = threading.Thread(target=self._run_loop, name="telegram-app", daemon=True)
        self._thread = thread
        thread.start()
        atexit.register(self.shutdown)

        if not wait:
            return False
        deadline = time.time() + timeout
        while not self._started and time.time() < deadline:
            if self._start_error:
                break
            time.sleep(0.2)
        return self._started

    def ensure_started_async(self) -> None:
        """Kick off a background start without blocking the caller.

        A failed start is retried, but not more often than every
        ``START_COOLDOWN_SECONDS`` -- /health may be polled frequently.
        """
        if self._started or self._starting:
            return
        if self._last_attempt_at and (time.time() - self._last_attempt_at) < START_COOLDOWN_SECONDS:
            return
        threading.Thread(target=self.start, kwargs={"wait": False},
                         name="telegram-boot", daemon=True).start()

    @property
    def retry_in_seconds(self) -> int:
        if self._started or not self._last_attempt_at:
            return 0
        remaining = START_COOLDOWN_SECONDS - (time.time() - self._last_attempt_at)
        return max(0, int(remaining))

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            application, _services = build_application(
                self.settings, for_webhook=(self.mode == "webhook")
            )
            self._app = application
            # Each Telegram-facing step is bounded: a hang becomes a visible,
            # logged error in /health (start_error) instead of "starting" forever.
            self._step = "initialize"
            logger.info("telegram start: initialize()")
            loop.run_until_complete(asyncio.wait_for(application.initialize(), START_STEP_TIMEOUT))
            self._step = "start"
            logger.info("telegram start: start()")
            loop.run_until_complete(asyncio.wait_for(application.start(), START_STEP_TIMEOUT))
            logger.info("telegram start: application running")
            if self.mode == "polling":
                loop.run_until_complete(
                    application.updater.start_polling(
                        allowed_updates=ALLOWED_UPDATES,
                        drop_pending_updates=self.settings.drop_pending_updates,
                        close_loop=False,
                    )
                )
                logger.info("long polling started")
            self._started = True
            self._started_at = time.time()
            logger.info("Telegram application started (mode=%s, bot=@%s)",
                        self.mode, self.bot_username)
            if self.mode == "webhook":
                # PTB's initialize()/start() do NOT run post_init. Register the webhook
                # and command menu in the background so a slow Telegram call can never
                # keep the bot in "starting" (which makes every update get a 503).
                schedule_webhook_setup(loop, application)
            loop.run_forever()
        except Exception as exc:  # pragma: no cover - environment specific
            step = getattr(self, "_step", "startup")
            detail = redact(str(exc)) or type(exc).__name__
            self._start_error = redact(f"{step} failed: {detail}")[:300]
            logger.exception("Telegram application failed to start: %s", self._start_error)
        finally:
            try:
                self._graceful_stop(loop)
            finally:
                try:
                    loop.close()
                except Exception:
                    pass
                self._started = False
                self._starting = False
                logger.info("Telegram application stopped")

    def _graceful_stop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._app is None:
            return
        try:
            if self.mode == "polling" and self._app.updater:
                loop.run_until_complete(self._app.updater.stop())
            loop.run_until_complete(self._app.stop())
            loop.run_until_complete(self._app.shutdown())
        except Exception as exc:  # pragma: no cover
            logger.debug("graceful shutdown issue: %s", exc)

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._lock:
            loop = self._loop
            if loop is None or not loop.is_running():
                self._started = False
                return
        try:
            future = asyncio.run_coroutine_threadsafe(self._async_shutdown(), loop)
            future.result(timeout=timeout)
        except Exception as exc:  # pragma: no cover
            logger.debug("shutdown error: %s", exc)
        finally:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass

    async def _async_shutdown(self) -> None:
        app = self._app
        if app is None:
            return
        try:
            if self.mode == "polling" and app.updater:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception as exc:  # pragma: no cover
            logger.debug("async shutdown issue: %s", exc)

    # ------------------------------------------------------------------ #
    # update ingestion (webhook path)
    # ------------------------------------------------------------------ #
    def process_update(self, update_data: dict[str, Any], *,
                       timeout: float = 25.0,
                       start_timeout: float = WEBHOOK_START_TIMEOUT_SECONDS) -> bool:
        """Hand a raw Telegram update dict to PTB. Returns ``True`` on success."""
        if not isinstance(update_data, dict) or "update_id" not in update_data:
            logger.warning("malformed update payload rejected")
            self._failed_updates += 1
            return False

        if not self._started:
            # a webhook must never hang for a minute: allow a short cold start and
            # let the caller ask Telegram to retry (503) if we are still booting
            self.start(wait=True, timeout=start_timeout)
        if not self._started or self._app is None or self._loop is None:
            self._failed_updates += 1
            raise ApplicationNotReady(self._start_error or "application not started")

        try:
            update = Update.de_json(update_data, self._app.bot)
        except Exception as exc:
            logger.warning("could not parse update: %s", redact(str(exc))[:200])
            self._failed_updates += 1
            return False

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._app.process_update(update), self._loop)
            future.result(timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("update %s timed out after %.0fs", update_data.get("update_id"), timeout)
            self._failed_updates += 1
            return False
        except Exception as exc:
            logger.error("update processing failed: %s", redact(str(exc))[:250])
            self._failed_updates += 1
            return False

        self._update_count += 1
        self._last_update_at = time.time()
        return True

    # ------------------------------------------------------------------ #
    # webhook helpers
    # ------------------------------------------------------------------ #
    def webhook_status(self) -> dict[str, Any]:
        """Live webhook information straight from Telegram (admin diagnostics)."""
        if self._app is None:
            return {"ok": False, "error": "application not started"}
        try:
            loop = self._loop
            if loop is None:
                return {"ok": False, "error": "event loop unavailable"}
            future = asyncio.run_coroutine_threadsafe(
                self._app.bot.get_webhook_info(), loop)
            info = future.result(timeout=15)
            return {
                "ok": True,
                "url": info.url,
                "pending_update_count": info.pending_update_count,
                "last_error_date": info.last_error_date,
                "last_error_message": info.last_error_message,
                "max_connections": info.max_connections,
                "allowed_updates": info.allowed_updates,
            }
        except Exception as exc:
            return {"ok": False, "error": redact(str(exc))[:200]}

    def set_webhook(self, *, delete_first: bool = False) -> dict[str, Any]:
        """(Re)register the webhook via the raw HTTP API (works before startup)."""
        url = self.settings.webhook_url
        if not url:
            return {"ok": False, "error": "PUBLIC_URL is not configured"}
        payload: dict[str, Any] = {
            "url": url,
            "allowed_updates": ALLOWED_UPDATES,
            "drop_pending_updates": self.settings.drop_pending_updates,
            "max_connections": self.settings.webhook_max_connections,
        }
        if self.settings.webhook_secret:
            payload["secret_token"] = self.settings.webhook_secret
        base = f"https://api.telegram.org/bot{self.settings.bot_token}"
        try:
            if delete_first:
                requests.post(f"{base}/deleteWebhook", timeout=15)
            response = requests.post(f"{base}/setWebhook", json=payload, timeout=20)
            data = response.json() if response.content else {}
            ok = bool(data.get("ok"))
            logger.info("setWebhook -> ok=%s description=%s", ok, data.get("description"))
            return {"ok": ok, "url": url, "description": data.get("description", "")}
        except Exception as exc:
            logger.error("setWebhook failed: %s", redact(str(exc))[:200])
            return {"ok": False, "error": redact(str(exc))[:200]}


# --------------------------------------------------------------------------- #
# singletons
# --------------------------------------------------------------------------- #
_manager: ApplicationManager | None = None
_manager_lock = threading.RLock()


def get_manager(mode: str = "webhook") -> ApplicationManager:
    global _manager
    with _manager_lock:
        if _manager is None or _manager.mode != mode:
            _manager = ApplicationManager(get_settings(), mode=mode)
        return _manager


def reset_manager() -> None:
    global _manager
    with _manager_lock:
        _manager = None


def process_update(update_data: dict[str, Any]) -> bool:
    """Convenience wrapper used by the Flask webhook."""
    return get_manager().process_update(update_data)


def shutdown_manager() -> None:
    manager = _manager
    if manager is not None:
        manager.shutdown()


# --------------------------------------------------------------------------- #
# local polling entry point
# --------------------------------------------------------------------------- #
def run_polling() -> None:
    """``python bot.py`` -- long polling, ideal for local development."""
    configure_logging(get_settings().log_level)
    settings = get_settings(strict=True)
    logger.info("starting INFO GROUP AI BOT in polling mode")
    logger.info("configuration: %s", settings.public_summary())
    manager = get_manager(mode="polling")
    if not manager.start(wait=True, timeout=45):
        logger.critical("could not start polling application: %s", manager.start_error)
        raise SystemExit(1)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("interrupted by user")
    finally:
        manager.shutdown()


def delete_webhook() -> None:
    """Remove any registered webhook (needed before switching to polling)."""
    settings = get_settings()
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{settings.bot_token}/deleteWebhook",
            json={"drop_pending_updates": True}, timeout=15)
        logger.info("deleteWebhook: %s", response.text[:200])
    except Exception as exc:
        logger.error("deleteWebhook failed: %s", redact(str(exc))[:200])


if __name__ == "__main__":  # pragma: no cover - manual entry point
    import sys

    if "--delete-webhook" in sys.argv:
        delete_webhook()
    run_polling()

"""Flask application: health endpoint + Telegram webhook receiver.

Endpoints
---------
``GET  /``              -> service info (JSON)
``GET  /health``        -> UptimeRobot-friendly health probe
``POST /webhook``       -> Telegram updates (secret-token protected)
``POST /telegram/webhook`` -> alias of /webhook
``GET  /set_webhook``   -> admin helper to (re)register the webhook
``GET  /version``       -> build/version information

The Telegram ``Application`` is started lazily (background thread) so the first
HTTP response is never blocked, except for ``/webhook`` which waits briefly so
the very first update is not lost.
"""

from __future__ import annotations

import hmac
import os
import time
from typing import Any

from flask import Flask, jsonify, request

import database
from config import Settings, get_settings
from utils.logger import configure_logging, get_logger, redact, register_secrets

logger = get_logger(__name__)

SERVICE_NAME = "INFO GROUP AI BOT"
VERSION = "1.0.0"
START_TIME = time.time()

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Server": "info-group-ai-bot",
}


def _manager():
    """Import lazily so tests can stub the Telegram layer."""
    import bot as bot_module
    return bot_module.get_manager()


def _application_not_ready() -> type[BaseException]:
    """The exception raised by the manager while the bot is still booting."""
    import bot as bot_module
    return bot_module.ApplicationNotReady


# --------------------------------------------------------------------------- #
# app factory
# --------------------------------------------------------------------------- #
def create_app(settings: Settings | None = None, *, bootstrap: bool = True) -> Flask:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    register_secrets(settings.secret_map().values())

    app = Flask(__name__)
    app.config["SETTINGS"] = settings
    app.config["JSON_SORT_KEYS"] = False
    app.url_map.strict_slashes = False

    if settings.problems:
        logger.critical("configuration problems detected: %s", settings.problems)
    for warning in settings.warnings:
        logger.warning("config warning: %s", warning)
    logger.info("starting %s v%s (env=%s)", SERVICE_NAME, VERSION, settings.environment)
    logger.info("configuration: %s", settings.public_summary())

    # ------------------------------------------------------------------ #
    # routes
    # ------------------------------------------------------------------ #
    @app.get("/")
    def index():
        return jsonify({
            "status": "ok",
            "service": SERVICE_NAME,
            "version": VERSION,
            "model": settings.groq_model,
            "webhook_path": settings.webhook_path,
            "docs": "See README.md for setup instructions.",
        })

    @app.get("/health")
    def health():
        """UptimeRobot endpoint -- always fast, never raises.

        It also (re)tries to start the Telegram application in the background, so
        a mis-timed cold start cannot leave the bot dead forever.
        """
        manager = _manager()
        if settings.autostart_bot:
            manager.ensure_started_async()   # also revives the bot after a crash
        try:
            db_health = database.health()
        except Exception as exc:  # pragma: no cover - defensive
            db_health = {"available": False, "error": redact(str(exc))[:160]}
        telegram = manager.stats()
        db_ok = bool(db_health.get("available"))
        # "degraded" tells the operator (and UptimeRobot keyword monitors) that
        # something is wrong while still answering HTTP 200, so an UptimeRobot
        # monitor does not bounce up and down on every cold start.
        bot_broken = (bool(telegram.get("start_error"))
                      and not telegram.get("started") and not telegram.get("starting"))
        payload: dict[str, Any] = {
            "status": "ok" if (settings.is_configured and db_ok and not bot_broken) else "degraded",
            "service": SERVICE_NAME,
            "version": VERSION,
            "uptime_seconds": int(time.time() - START_TIME),
            "environment": settings.environment,
            "ai": {
                "enabled": bool(settings.groq_enabled and settings.groq_api_key),
                "model": settings.groq_model,
            },
            "database": {
                "available": db_ok,
                "latency_ms": db_health.get("latency_ms"),
            },
            "telegram": telegram,
        }
        if settings.problems:
            payload["problems"] = list(settings.problems)
        return jsonify(payload), 200

    @app.get("/version")
    def version():
        return jsonify({
            "service": SERVICE_NAME,
            "version": VERSION,
            "python": os.sys.version.split()[0],
            "model": settings.groq_model,
        })

    @app.get("/set_webhook")
    def set_webhook():
        """Convenience endpoint for initial setup / webhook repair.

        Protected by ``WEBHOOK_SECRET``: ``/set_webhook?token=<WEBHOOK_SECRET>``
        (or the ``X-Telegram-Bot-Api-Secret-Token`` header).
        """
        if not _authorized(request, settings):
            logger.warning("unauthorized /set_webhook attempt from %s", request.remote_addr)
            return jsonify({"ok": False, "error": "unauthorized"}), 403
        if not settings.webhook_url:
            return jsonify({"ok": False,
                            "error": "PUBLIC_URL is not set"}), 400
        result = _manager().set_webhook(delete_first=_truthy(request.args.get("delete_first")))
        return jsonify(result), (200 if result.get("ok") else 502)

    @app.route(settings.webhook_path, methods=["POST"])
    @app.route("/telegram/webhook", methods=["POST"])
    def webhook():
        """Receive Telegram updates."""
        # A misconfigured deployment would reject every update anyway; report it
        # first so the operator sees the real problem in Telegram's webhook info.
        if settings.problems:
            logger.error("webhook call refused, configuration problems: %s", settings.problems)
            return jsonify({"ok": False, "error": "service misconfigured",
                            "problems": list(settings.problems)}), 503

        if not _authorized(request, settings):
            logger.warning("rejected webhook call with bad/missing secret from %s",
                           request.remote_addr)
            return jsonify({"ok": False, "error": "unauthorized"}), 403

        data = request.get_json(silent=True)
        if not isinstance(data, dict) or "update_id" not in data:
            logger.warning("malformed webhook payload: %s", str(data)[:200])
            return jsonify({"ok": False, "error": "invalid update"}), 400

        manager = _manager()
        try:
            if settings.autostart_bot:
                manager.ensure_started_async()
            processed = manager.process_update(data)
        except _application_not_ready() as exc:
            # the bot is still booting (cold start): tell Telegram to deliver the
            # update again instead of silently losing it
            logger.warning("webhook hit while the bot was starting: %s", str(exc)[:200])
            return jsonify({"ok": False, "processed": False, "reason": "bot starting"}), 503
        except Exception as exc:
            logger.exception("webhook processing error: %s", redact(str(exc))[:250])
            # 200 so Telegram does not retry a poison update forever
            return jsonify({"ok": True, "processed": False,
                            "note": "update dropped after internal error"}), 200
        return jsonify({"ok": True, "processed": bool(processed)}), 200

    # ------------------------------------------------------------------ #
    # error handling (never leak stack traces or secrets to the caller)
    # ------------------------------------------------------------------ #
    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"ok": False, "error": "not found"}), 404

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return jsonify({"ok": False, "error": "method not allowed"}), 405

    @app.errorhandler(500)
    def server_error(error):  # pragma: no cover - defensive
        logger.error("unhandled application error: %s", redact(str(error))[:250])
        return jsonify({"ok": False, "error": "internal server error"}), 500

    @app.after_request
    def add_headers(response):
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response

    if bootstrap and settings.autostart_bot and settings.is_configured:
        try:
            _manager().ensure_started_async()
        except Exception as exc:  # pragma: no cover
            logger.warning("background bot start failed: %s", exc)

    return app


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _authorized(request_obj: Any, settings: Settings) -> bool:
    """Validate the Telegram webhook secret token."""
    if not settings.webhook_secret:
        if settings.webhook_require_secret:
            return False
        logger.warning("WEBHOOK_SECRET not set and WEBHOOK_REQUIRE_SECRET=false "
                       "-- webhook is unprotected")
        return True
    header_secret = request_obj.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    query_token = request_obj.args.get("token", "")
    return any(
        submitted and hmac.compare_digest(str(submitted), settings.webhook_secret)
        for submitted in (header_secret, query_token)
    )


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# module-level app (gunicorn: ``gunicorn app:app``)
# --------------------------------------------------------------------------- #
app = create_app()


if __name__ == "__main__":  # pragma: no cover - local development
    settings = app.config["SETTINGS"]
    port = int(os.environ.get("PORT", 5000))
    logger.info("starting Flask development server on 0.0.0.0:%s", port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

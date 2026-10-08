"""Self-diagnosis ("doctor") for ``GET /diagnose?token=<WEBHOOK_SECRET>``.

Answers the one question a group admin actually has when the bot stays silent:

    "কেন bot উত্তর দিচ্ছে না?"

It inspects, from the inside of the running service:

1. configuration (missing env vars, PUBLIC_URL vs. the URL Telegram has),
2. the PostgreSQL connection,
3. ``getMe`` -- including **privacy mode** (``can_read_all_group_messages``),
4. ``getWebhookInfo`` -- registered URL, pending updates, last Telegram error,
5. group membership of the bot and of the memory admin,
6. registered slash-commands.

Secrets are never returned: only ``configured / not configured`` flags.
"""

from __future__ import annotations

from typing import Any, Callable

import requests

from config import Settings
from utils.logger import get_logger, redact

logger = get_logger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
HTTP_TIMEOUT = 8.0

OK = "ok"
WARN = "warn"
FAIL = "fail"


def _telegram_call(token: str, method: str, **params: Any) -> dict[str, Any]:
    """Call one Telegram Bot API method. Never raises."""
    try:
        response = requests.post(TELEGRAM_API.format(token=token, method=method),
                                 json=params or None, timeout=HTTP_TIMEOUT)
        data = response.json() if response.content else {}
    except Exception as exc:  # network down, DNS, timeout ...
        return {"ok": False, "error": redact(str(exc))[:200]}
    if not isinstance(data, dict):
        return {"ok": False, "error": "unexpected Telegram response"}
    return data


def _check(check_id: str, status: str, detail: str, hint_bn: str = "",
           **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": check_id, "status": status, "detail": detail}
    if hint_bn:
        payload["hint_bn"] = hint_bn
    if extra:
        payload.update(extra)
    return payload


def _classify_webhook_error(message: str, registered_url: str,
                            expected_url: str) -> tuple[str, str]:
    """Turn Telegram's ``last_error_message`` into (status, Bengali fix hint)."""
    text = (message or "").lower()
    if not text:
        return OK, ""
    if "404" in text or "not found" in text:
        hint = ("Telegram যে path-এ পাঠাচ্ছে সেটি অ্যাপে নেই। সাধারণত PUBLIC_URL-এ "
                "ভুলে '/webhook' লিখে দেওয়া হয় (তাহলে path দাঁড়ায় /webhook/webhook)। "
                "Render → Environment → PUBLIC_URL-এ শুধু https://আপনার-service.onrender.com "
                "দিন (শেষে কোনো /webhook নয়), তারপর /set_webhook?token=... আবার খুলুন।")
        if registered_url and registered_url.lower().endswith("/webhook/webhook"):
            hint = ("নিশ্চিত হয়েছে: registered URL-এ '/webhook' দুইবার আছে। PUBLIC_URL-এ "
                    "শুধু base URL দিন — কোড নিজেই /webhook যোগ করবে।")
        return FAIL, hint
    if "403" in text or "forbidden" in text:
        return FAIL, ("আপনার অ্যাপ update গ্রহণ করছে না কারণ WEBHOOK_SECRET মিলছে না। "
                      "Render → Environment-এ WEBHOOK_SECRET-এর মান হুবহু কপি করে "
                      "/set_webhook?token=<সেই মান> আবার খুলুন (এতে Telegram-এ secret_token "
                      "আবার সেট হবে)।")
    if any(word in text for word in ("502", "503", "504", "bad gateway", "timeout", "timed out")):
        return WARN, ("আপনার সার্ভিস ঘুমিয়ে ছিল বা এখনো চালু হচ্ছে (Render Free ১৫ মিনিট "
                      "idle-এ ঘুমায়)। UptimeRobot দিয়ে /health প্রতি ৫ মিনিটে ping করুন; "
                      "সাময়িক 503 হলে Telegram নিজেই আবার পাঠায়।")
    if any(word in text for word in ("refused", "unreachable", "resolve", "ssl", "certificate")):
        return FAIL, ("Telegram আপনার URL-এ পৌঁছাতে পারছে না। PUBLIC_URL-এর service নাম "
                      "ঠিক আছে কি না (Render ড্যাশবোর্ডের উপরের URL-এর সাথে মিলিয়ে) দেখুন।")
    if "unauthorized" in text or "401" in text:
        return FAIL, "BOT_TOKEN ভুল বা revoke হয়েছে — BotFather থেকে নতুন token নিন।"
    return WARN, (f"Telegram-এর সর্বশেষ error: {message}. বিস্তারিত: "
                  "docs/TROUBLESHOOTING.md")


def run_diagnostics(settings: Settings, *, manager: Any = None,
                    database: Any = None,
                    telegram_call: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Collect every check. Returns a JSON-serialisable report."""
    call = telegram_call or _telegram_call
    checks: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # 1. configuration
    # ------------------------------------------------------------------ #
    expected_url = settings.webhook_url
    checks.append(_check(
        "configuration",
        OK if settings.is_configured else FAIL,
        ("all required settings present" if settings.is_configured
         else f"problems: {list(settings.problems)}"),
        "" if settings.is_configured else
        "Render → Environment-এ সমস্যাগুলো ঠিক করুন, তারপর Save (redeploy হবে)।",
        problems=list(settings.problems), warnings=list(settings.warnings),
    ))
    checks.append(_check(
        "public_url",
        OK if expected_url else FAIL,
        f"PUBLIC_URL -> expected webhook URL: {expected_url or '(not set)'}",
        "" if expected_url else
        "PUBLIC_URL = https://আপনার-service.onrender.com (শুধু base, শেষে /webhook নয়)।",
    ))

    # ------------------------------------------------------------------ #
    # 2. database
    # ------------------------------------------------------------------ #
    db_health: dict[str, Any] = {}
    if database is not None:
        try:
            db_health = database.health()
        except Exception as exc:
            db_health = {"available": False, "error": redact(str(exc))[:160]}
    db_ok = bool(db_health.get("available"))
    checks.append(_check(
        "database",
        OK if db_ok else FAIL,
        (f"connected (PostgreSQL {db_health.get('server_version', '?')}, "
         f"{db_health.get('latency_ms', '?')} ms)"
         if db_ok else f"not reachable: {db_health.get('error', 'unknown')}"),
        "" if db_ok else
        ("Supabase-এর 'Session pooler' URI ব্যবহার করুন (Direct connection IPv6-only, "
         "Render IPv4-only) এবং schema.sql চালানো আছে কি না দেখুন।"),
    ))

    # ------------------------------------------------------------------ #
    # 3/4/5. Telegram
    # ------------------------------------------------------------------ #
    if not settings.bot_token:
        checks.append(_check("telegram", FAIL, "BOT_TOKEN is not set",
                             "Render → Environment-এ BOT_TOKEN যোগ করুন।"))
        return _finish(checks, expected_url)

    me = call(settings.bot_token, "getMe")
    bot_id: int | None = None
    if not me.get("ok"):
        checks.append(_check(
            "bot_token", FAIL,
            f"getMe failed: {me.get('description') or me.get('error')}",
            "BOT_TOKEN ভুল/revoke হয়েছে কি না দেখুন (BotFather → /mybots → API Token)।"))
    else:
        result = me.get("result") or {}
        bot_id = result.get("id")
        reads_all = bool(result.get("can_read_all_group_messages"))
        checks.append(_check(
            "bot_token", OK, f"token works, bot = @{result.get('username')}"))
        checks.append(_check(
            "privacy_mode", OK if reads_all else WARN,
            ("privacy mode is DISABLED (bot sees all group messages)"
             if reads_all else
             "privacy mode is ENABLED -- bot does not receive normal group messages"),
            "" if reads_all else
            ("BotFather → /setprivacy → আপনার bot → Disable, তারপর গ্রুপ থেকে bot "
             "remove করে আবার add করুন (এটি ছাড়া admin-এর কথা মনে রাখা যাবে না)।"),
        ))

    info = call(settings.bot_token, "getWebhookInfo")
    registered_url = ""
    if not info.get("ok"):
        checks.append(_check("webhook", FAIL,
                             f"getWebhookInfo failed: {info.get('description') or info.get('error')}"))
    else:
        data = info.get("result") or {}
        registered_url = data.get("url") or ""
        error_message = data.get("last_error_message") or ""
        status, hint = _classify_webhook_error(error_message, registered_url, expected_url)
        if not registered_url:
            status, hint = FAIL, ("Telegram-এ কোনো webhook সেট নেই (long polling-ও চলছে না)। "
                                  "PUBLIC_URL ঠিক করে /set_webhook?token=<WEBHOOK_SECRET> খুলুন।")
        elif expected_url and registered_url.rstrip("/") != expected_url.rstrip("/"):
            status, hint = FAIL, (f"Telegram অন্য ঠিকানায় পাঠাচ্ছে ({registered_url})। "
                                 f"PUBLIC_URL ঠিক করে /set_webhook?token=<WEBHOOK_SECRET> "
                                 "আবার খুলুন।")
        detail = (f"url={registered_url or '(none)'}, pending_update_count="
                  f"{data.get('pending_update_count')}, last_error={error_message or 'none'}")
        checks.append(_check("webhook", status, detail, hint,
                             registered_url=registered_url, expected_url=expected_url,
                             pending_update_count=data.get("pending_update_count"),
                             last_error_message=error_message))

    # group membership (only when GROUP_ID is configured)
    if settings.group_id and bot_id:
        chat = call(settings.bot_token, "getChat", chat_id=settings.group_id)
        if not chat.get("ok"):
            checks.append(_check(
                "group", FAIL,
                f"getChat({settings.group_id}) failed: {chat.get('description') or chat.get('error')}",
                "GROUP_ID ভুল হতে পারে। গ্রুপে /whoami পাঠালে সঠিক chat id পাওয়া যাবে।"))
        else:
            chat_result = chat.get("result") or {}
            checks.append(_check("group", OK,
                                 f"group ok: {chat_result.get('title')} "
                                 f"({chat_result.get('type')})"))
            member = call(settings.bot_token, "getChatMember",
                          chat_id=settings.group_id, user_id=bot_id)
            member_status = ((member.get("result") or {}).get("status")
                             if member.get("ok") else "error")
            in_group = member_status in {"member", "administrator", "creator", "restricted"}
            checks.append(_check(
                "bot_in_group", OK if in_group else FAIL,
                f"bot membership status = {member_status}",
                "" if in_group else "গ্রুপে bot-কে member হিসেবে add করুন।",
            ))

    # memory admin sanity
    admin_ids = list(settings.target_admin_ids)
    checks.append(_check(
        "target_admin", OK if admin_ids else WARN,
        f"TARGET_ADMIN_ID = {admin_ids or '(not set)'}",
        "" if admin_ids else
        ("TARGET_ADMIN_ID না দিলে bot কারো কথা মনে রাখবে না। গ্রুপে /whoami দিয়ে id নিন, "
         "তারপর Render-এ যোগ করে Save করুন (এটি ছাড়া Q&A memory তৈরি হবে না)।"),
    ))

    commands = call(settings.bot_token, "getMyCommands")
    if commands.get("ok"):
        count = len(commands.get("result") or [])
        checks.append(_check("commands", OK if count else WARN,
                             f"{count} slash-commands registered",
                             "" if count else
                             "scripts/set_commands.py বা bot restart দিলে কমান্ড মেনু বসবে।"))

    return _finish(checks, expected_url, registered_url=registered_url)


def _finish(checks: list[dict[str, Any]], expected_url: str,
            registered_url: str = "") -> dict[str, Any]:
    failed = [c for c in checks if c["status"] == FAIL]
    warned = [c for c in checks if c["status"] == WARN]
    if failed:
        verdict = "broken"
        summary_bn = "❌ Bot কাজ করবে না — নিচের ❌ চেকগুলো ঠিক করুন।"
    elif warned:
        verdict = "degraded"
        summary_bn = "⚠️ Bot চলছে, তবে কিছু বিষয় ঠিক করা দরকার।"
    else:
        verdict = "healthy"
        summary_bn = "✅ সব ঠিক আছে — bot উত্তর দিতে প্রস্তুত।"
    return {
        "ok": not failed,
        "verdict": verdict,
        "summary_bn": summary_bn,
        "expected_webhook_url": expected_url,
        "registered_webhook_url": registered_url,
        "checks": checks,
        "fixes_bn": [c["hint_bn"] for c in checks if c.get("hint_bn")],
    }

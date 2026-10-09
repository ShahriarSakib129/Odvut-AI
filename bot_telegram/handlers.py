"""Telegram handlers: commands, group behaviour and the answer flow.

Command matrix
--------------
everyone : ``/start`` ``/help`` ``/ask`` ``/status`` ``/memory`` ``/whoami`` ``/ping``
admins   : ``/stats`` ``/memory_stats`` ``/clear_memory`` ``/set`` ``/reload`` ``/adminhelp``

Group behaviour (requirement #16): the bot is silent by default.
It only answers when

* a member uses ``/ask <question>``
* a member mentions the bot (``TRIGGER_ON_MENTION``)
* a member replies to one of the bot's messages (``TRIGGER_ON_REPLY``)
* a configured keyword appears (``TRIGGER_KEYWORDS``)

Everything else is only *tracked* (admin memory / Q&A pairing), never answered.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Sequence

try:  # python-telegram-bot is a hard dependency
    from telegram import (
        BotCommand,
        BotCommandScopeAllGroupChats,
        BotCommandScopeAllPrivateChats,
        Message,
        Update,
    )
    from telegram.constants import ChatAction, ChatType, ParseMode
    from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError, TimedOut
    from telegram.ext import (
        Application,
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except Exception as exc:  # pragma: no cover - only when dependency missing
    raise ImportError(
        "python-telegram-bot is required: pip install -r requirements.txt"
    ) from exc

from config import Settings, get_settings
from services import Services, build_services, run_maintenance, startup_tasks
from utils.helpers import chunk_text, human_count, human_time
from utils.logger import get_logger
from utils.text import classify_topic, truncate

from .permissions import GROUP_CHAT_TYPES, clean_question, describe_actor

if TYPE_CHECKING:  # pragma: no cover
    from ai.responder import AnswerResult

logger = get_logger(__name__)

ALLOWED_UPDATES: list[str] = ["message"]

PUBLIC_COMMANDS = ("start", "help", "ask", "status", "memory", "whoami", "ping")
ADMIN_COMMANDS = ("stats", "memory_stats", "clear_memory", "set", "reload", "maintenance",
                  "adminhelp")
TOGGLE_KEYS = {
    "ai_enabled": "AI উত্তর চালু/বন্ধ",
    "auto_reply_enabled": "গ্রুপে অটো-উত্তর চালু/বন্ধ",
    "memory_enabled": "Admin memory সংগ্রহ চালু/বন্ধ",
    "qa_pairing_enabled": "প্রশ্ন-উত্তর pairing চালু/বন্ধ",
    "reply_footer_enabled": "উত্তরে AI disclosure footer",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def services_of(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.bot_data["services"]


def _is_duplicate(update: Update, services: Services) -> bool:
    update_id = getattr(update, "update_id", None)
    if update_id is None:
        return False
    return services.dedupe.seen(update_id)


async def _safe_send(message: Message, text: str, *, reply: bool = True,
                     allow_parse_mode: bool = False) -> bool:
    """Send a (possibly long) message without ever raising.

    Telegram errors (blocked bot, deleted message, parse errors, flood control)
    are logged and swallowed -- a failing reply must never crash the worker.
    """
    if not text or message is None:
        return False
    chunks = chunk_text(text, 4000)
    ok = True
    for index, chunk in enumerate(chunks):
        first = index == 0 and reply
        for attempt in (1, 2):
            try:
                if first:
                    await message.reply_text(chunk, disable_web_page_preview=True)
                elif allow_parse_mode and attempt == 1:
                    await message.chat.send_message(
                        chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                else:
                    await message.chat.send_message(chunk, disable_web_page_preview=True)
                break
            except RetryAfter as exc:
                wait = float(getattr(exc, "retry_after", 3) or 3) + 0.5
                logger.warning("flood control: waiting %.1fs", wait)
                await asyncio.sleep(wait)
            except Forbidden:
                logger.warning("cannot send to chat %s: bot blocked/kicked", message.chat_id)
                return False
            except BadRequest as exc:
                text_error = str(exc).lower()
                if "parse" in text_error and allow_parse_mode and attempt == 1:
                    continue  # retry as plain text
                if "message to be replied not found" in text_error and first:
                    first = False
                    continue  # the original message was deleted: send without reply
                logger.warning("send failed (bad request): %s", exc)
                ok = False
                break
            except TimedOut:
                logger.warning("send timed out (attempt %s)", attempt)
                ok = False
                break
            except TelegramError as exc:
                logger.warning("send failed: %s", exc)
                ok = False
                break
        else:
            logger.warning("giving up on chunk %s", index + 1)
            ok = False
    return ok


async def _typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except TelegramError:
        pass


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return ``True`` when the caller may use admin commands."""
    services = services_of(context)
    actor = services.permissions.actor_from(update)
    if (services.settings.allow_group_admins and actor.chat_id
            and actor.chat_type in GROUP_CHAT_TYPES and not actor.is_group_admin
            and not actor.is_bot_admin):
        await services.permissions.refresh_group_admins(context.bot, actor.chat_id)
        actor = services.permissions.actor_from(update)
    allowed, reason = services.permissions.authorize_admin(actor)
    if allowed:
        logger.info("admin command authorized (%s): %s", reason, describe_actor(actor))
        return True
    logger.warning("admin command denied: %s", describe_actor(actor))
    message = update.effective_message
    if message:
        await _safe_send(
            message,
            "⛔ এই command শুধু bot admin-এর জন্য।\n"
            f"তোমার Telegram user id: {actor.user_id}\n"
            "(এটি ADMIN_ID-তে যোগ করতে হবে।)",
        )
    return False


def _actor_ids(update: Update) -> tuple[int | None, int | None, str | None]:
    user = update.effective_user
    chat = update.effective_chat
    return (getattr(user, "id", None), getattr(chat, "id", None),
            getattr(user, "username", None))


# --------------------------------------------------------------------------- #
# answer flow (shared by /ask, mentions, replies, private chat)
# --------------------------------------------------------------------------- #
def memory_chat_id(services: Services, chat: Any) -> int:
    """Which chat's memory is used to answer.

    In a group that is the group itself. In a private chat we fall back to the
    configured ``GROUP_ID`` so a member can ask the same bot 1:1.
    """
    chat_type = getattr(chat, "type", "private")
    if chat_type in GROUP_CHAT_TYPES:
        return int(chat.id)
    return int(services.settings.group_id or chat.id)


async def _answer_question(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           question: str, *, source: str = "trigger") -> None:
    services = services_of(context)
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None:
        return

    settings = services.settings
    answer_chat_id = memory_chat_id(services, chat)
    cleaned = clean_question(question, getattr(context.bot, "username", None),
                             limit=settings.max_question_chars)
    if not cleaned:
        await _safe_send(message, "🤔 প্রশ্নটা লিখতে ভুলে গেলে। যেমন: `/ask BTC এখন কেমন?`".replace("`", ""))
        return

    if not services.runtime_flag("ai_enabled", True):
        await _safe_send(message, "ℹ️ AI উত্তর এই মুহূর্তে বন্ধ আছে (admin)।")
        return

    user_id = getattr(user, "id", None)
    if user_id and not services.rate_limiter.allow(user_id):
        wait = services.rate_limiter.retry_after(user_id)
        logger.info("rate limited user=%s source=%s", user_id, source)
        await _safe_send(message, f"⏳ একটু ধীরে 🙂 {int(wait) + 1} সেকেন্ড পর আবার প্রশ্ন করো।")
        return

    if settings.typing_indicator:
        await _typing(context, chat.id)

    include_disclosure = chat.type in GROUP_CHAT_TYPES

    started = time.time()
    try:
        result = await asyncio.to_thread(
            services.responder.answer,
            cleaned,
            chat_id=answer_chat_id,
            user_id=user_id,
            username=getattr(user, "username", None),
            include_disclosure=include_disclosure,
        )
    except Exception as exc:  # pragma: no cover - the service is defensive already
        logger.exception("answer pipeline crashed: %s", exc)
        await _safe_send(message, "😕 উত্তর তৈরি করতে সমস্যা হয়েছে। একটু পরে আবার চেষ্টা করো।")
        return

    logger.info(
        "answered source=%s user=%s chat=%s cached=%s memory=%s err=%s latency=%sms",
        source, user_id, chat.id, result.cached, result.used_memory, result.error_kind,
        int((time.time() - started) * 1000),
    )
    if user_id:
        try:
            await asyncio.to_thread(services.db.increment_user_counter, user_id, "ai_answer_count")
        except Exception:
            pass
    await _safe_send(message, result.text)


# --------------------------------------------------------------------------- #
# public commands
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_of(context)
    settings = services.settings
    chat = update.effective_chat
    if chat and chat.type in GROUP_CHAT_TYPES:
        text = (
            f"👋 <b>{settings.bot_name}</b> গ্রুপে active.\n\n"
            "এই গ্রুপের Admin-এর আগের উত্তর ও বিশ্লেষণের ভিত্তিতে আমি প্রশ্নের উত্তর দিই।\n\n"
            "ব্যবহার:\n"
            "• <code>/ask BTC এখন কেমন?</code>\n"
            f"• অথবা আমাকে mention করো: @{getattr(context.bot, 'username', 'bot')} প্রশ্ন...\n"
            "• আমার যেকোনো message-এ reply দিয়েও প্রশ্ন করতে পারো\n\n"
            "<i>আমি Admin নই — শুধু Admin-এর শেয়ার করা তথ্য অনুযায়ী উত্তর দিই।</i>"
        )
    else:
        text = (
            f"👋 স্বাগতম! আমি <b>{settings.bot_name}</b>।\n\n"
            "গ্রুপের Admin যেসব ব্যাখ্যা/opinion শেয়ার করেছেন, তার ভিত্তিতে প্রশ্নের উত্তর দিই।\n\n"
            "যেভাবে প্রশ্ন করবে:\n"
            "• <code>/ask বিটকয়েন এখন কেমন?</code>\n"
            "• অথবা এখানে সরাসরি লিখে দাও — আমি উত্তর দেবো\n\n"
            "⚠️ আমি কোনো financial advice দিই না, আর Admin-এর নামে নতুন কিছু বানিয়ে বলি না।"
        )
    await _safe_send(update.effective_message, text, allow_parse_mode=True)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_of(context)
    text = (
        f"🤖 <b>{services.settings.bot_name}</b> — সাহায্যের তালিকা\n\n"
        "<b>সবার জন্য:</b>\n"
        "/ask প্রশ্ন — Admin-এর তথ্যের ভিত্তিতে উত্তর\n"
        "/memory — আমি কী কী মনে রেখেছি (সারসংক্ষেপ)\n"
        "/status — bot ও AI service স্ট্যাটাস\n"
        "/whoami — তোমার Telegram id ও chat id (setup-এর জন্য দরকারি)\n"
        "/ping — bot সাড়া দিচ্ছে কি না\n"
        "\nগ্রুপে bot-কে mention বা reply করেও প্রশ্ন করা যায়। "
        "বাকি সময় bot চুপ থাকে, শুধু Admin-এর কথা memory-তে জমা করে।\n\n"
        "<b>মনে রাখো:</b> আমি Admin নই। কোনো তথ্য memory-তে না থাকলে সেটা স্পষ্টভাবে বলব।"
    )
    if services.permissions.is_bot_admin(getattr(update.effective_user, "id", None)):
        text += (
            "\n\n<b>Admin commands:</b>\n"
            "/stats, /memory_stats, /clear_memory, /set, /reload, /adminhelp"
        )
    await _safe_send(update.effective_message, text, allow_parse_mode=True)


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_of(context)
    question = " ".join(context.args or []) if context.args else ""
    if not question:
        raw = getattr(update.effective_message, "text", "") or ""
        question = raw.split(" ", 1)[1] if " " in raw else ""
    if not question.strip():
        await _safe_send(
            update.effective_message,
            "ব্যবহার: /ask তোমার প্রশ্ন\nউদাহরণ: /ask BTC এখন entry নেওয়া যাবে?",
        )
        return
    if not services.permissions.in_target_group(getattr(update.effective_chat, "id", None)):
        await _safe_send(update.effective_message, "ℹ️ এই bot শুধু নির্দিষ্ট গ্রুপের জন্য সেট করা।")
        return
    await _answer_question(update, context, question, source="command")


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    started = time.time()
    await _safe_send(update.effective_message,
                     f"🏓 পং! {(time.time() - started) * 1000:.0f} ms")


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_of(context)
    user = update.effective_user
    chat = update.effective_chat
    actor = services.permissions.actor_from(update)
    roles = []
    if actor.is_bot_admin:
        roles.append("Bot Admin")
    if actor.is_target_admin:
        roles.append("Target Admin (memory source)")
    if actor.is_group_admin:
        roles.append("Group Admin")
    text = (
        "🪪 <b>তোমার তথ্য</b>\n"
        f"user id: <code>{getattr(user, 'id', '?')}</code>\n"
        f"username: @{getattr(user, 'username', '—')}\n"
        f"chat id: <code>{getattr(chat, 'id', '?')}</code> ({getattr(chat, 'type', '?')})\n"
        f"role: {', '.join(roles) if roles else 'Member'}\n\n"
        "<b>Setup-এ কোথায় বসাবে:</b>\n"
        f"• BOT admin হলে → ADMIN_ID=<code>{getattr(user, 'id', '?')}</code>\n"
        f"• এই গ্রুপ হলে → GROUP_ID=<code>{getattr(chat, 'id', '?')}</code>\n"
        f"• Admin-এর কথা memory করতে হলে → TARGET_ADMIN_ID=<code>{getattr(user, 'id', '?')}</code>"
    )
    await _safe_send(update.effective_message, text, allow_parse_mode=True)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_of(context)
    is_admin = services.permissions.is_bot_admin(getattr(update.effective_user, "id", None))
    try:
        stats = await asyncio.to_thread(services.db.get_stats)
    except Exception:
        stats = {}
    ai = services.responder.health() if services.responder else {"enabled": False}
    lines = [
        f"📊 <b>{services.settings.bot_name}</b>",
        f"status: {'🟢 running' if services.ready else '🟡 degraded'}",
        f"AI: {'✅ ' + services.settings.groq_model if ai.get('enabled') else '⛔ disabled'}",
        f"database: {'✅ connected' if services.db_ready() else '⛔ unavailable'}",
        f"memory: {human_count(stats.get('admin_messages', 0))} admin messages, "
        f"{human_count(stats.get('qa_pairs', 0))} Q&A",
        f"known users: {human_count(stats.get('users', 0))}",
    ]
    if is_admin:
        db_health = await asyncio.to_thread(services.db.health)
        ai_status = await asyncio.to_thread(services.groq.health_check)
        cache = services.cache.stats() if services.cache else {}
        lines += [
            "",
            "<b>Admin detail</b>",
            f"model: <code>{services.settings.groq_model}</code>",
            f"db latency: {db_health.get('latency_ms')} ms | pg_trgm: {db_health.get('pg_trgm')}",
            f"db missing tables: {db_health.get('missing_tables') or 'none'}",
            f"groq check: {'✅' if ai_status.get('ok') else '⚠️ ' + str(ai_status.get('error_kind'))}",
            f"cache: {cache.get('entries', 0)} entries, hit-rate {cache.get('hit_rate', 0)}",
            f"latest memory: {human_time(stats.get('latest_memory'))}",
            f"open questions: {stats.get('open_questions', 0)} | "
            f"unanswered: {stats.get('unanswered_questions', 0)}",
            f"ai calls (24h): {(stats.get('ai_usage') or {}).get('calls', 0)}",
            f"webhook: <code>{services.settings.webhook_url or 'not configured'}</code>",
        ]
        if services.errors:
            lines.append(f"warnings: {', '.join(services.errors)}")
    await _safe_send(update.effective_message, "\n".join(lines), allow_parse_mode=True)


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_of(context)
    message = update.effective_message
    user = update.effective_user
    is_admin = services.permissions.is_bot_admin(getattr(user, "id", None))
    chat_id = update.effective_chat.id
    question = " ".join(context.args or []) if context.args else ""

    if question:
        await _typing(context, chat_id)
        try:
            result, memory = await asyncio.to_thread(
                services.responder.answer_with_context, question, chat_id=chat_id,
                user_id=getattr(user, "id", None),
            )
        except Exception as exc:
            logger.warning("/memory lookup failed: %s", exc)
            await _safe_send(message, "⚠️ memory খুঁজতে সমস্যা হয়েছে।")
            return
        lines = [f"🔎 <b>\"{truncate(question, 80)}\" — memory মিল</b>",
                 f"admin messages: {len(memory.admin_memories)} | Q&A: {len(memory.qa_pairs)}"]
        for item in memory.qa_pairs[:3]:
            date = item.answer_timestamp.strftime("%d %b %Y") if item.answer_timestamp else "?"
            lines.append(
                f"\n• <b>Q&A</b> ({date}, score {item.score:.2f})\n"
                f"  প্রশ্ন: {truncate(item.question, 120)}\n"
                f"  উত্তর: {truncate(item.answer, 200)}"
            )
        for item in memory.admin_memories[:3]:
            date = item.timestamp.strftime("%d %b %Y") if item.timestamp else "?"
            lines.append(f"\n• <b>Admin</b> ({date}, score {item.score:.2f}): "
                         f"{truncate(item.text, 200)}")
        if not memory.item_count:
            lines.append("\nএই প্রশ্নের সাথে মেলে এমন কিছু memory পাওয়া যায়নি।")
        await _safe_send(message, "\n".join(lines), allow_parse_mode=True)
        return

    try:
        stats = await asyncio.to_thread(services.db.get_stats)
        recent_qa = await asyncio.to_thread(services.db.recent_qa_pairs, chat_id, 3)
        recent_admin = await asyncio.to_thread(services.db.recent_admin_messages, chat_id, 3)
    except Exception as exc:
        logger.warning("/memory stats failed: %s", exc)
        await _safe_send(message, "⚠️ memory তথ্য আনতে সমস্যা হয়েছে (database)।")
        return

    lines = [
        "🧠 <b>আমি যা মনে রেখেছি</b>",
        f"• Admin messages: {human_count(stats.get('admin_messages', 0))}",
        f"• Q&A memory: {human_count(stats.get('qa_pairs', 0))}",
        f"• সর্বশেষ memory: {human_time(stats.get('latest_memory'))}",
    ]
    if recent_qa:
        lines.append("\n<b>সাম্প্রতিক প্রশ্ন-উত্তর:</b>")
        for row in recent_qa:
            date = row.get("answer_timestamp")
            lines.append(f"• {human_time(date)} — {truncate(row.get('question_text') or '', 90)}")
    if recent_admin:
        lines.append("\n<b>Admin-এর সাম্প্রতিক বক্তব্য:</b>")
        for row in recent_admin:
            limit = 160 if is_admin else 80
            lines.append(f"• {human_time(row.get('message_timestamp'))} — "
                         f"{truncate(row.get('message_text') or '', limit)}")
    lines.append(
        "\n<i>বিস্তারিত দেখতে: /memory তোমার প্রশ্ন</i>" if not is_admin
        else "\n<i>স্কোরসহ দেখতে: /memory তোমার প্রশ্ন | বিস্তারিত: /memory_stats</i>"
    )
    await _safe_send(message, "\n".join(lines), allow_parse_mode=True)


# --------------------------------------------------------------------------- #
# admin commands
# --------------------------------------------------------------------------- #
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    services = services_of(context)
    stats = await asyncio.to_thread(services.db.get_stats)
    db_health = await asyncio.to_thread(services.db.health)
    usage = stats.get("ai_usage") or {}
    lines = [
        "📈 <b>INFO GROUP AI BOT — Statistics</b>",
        f"• admin messages: {human_count(stats.get('admin_messages', 0))}",
        f"• Q&A memory: {human_count(stats.get('qa_pairs', 0))}",
        f"• known users: {human_count(stats.get('users', 0))}",
        f"• logged messages: {human_count(stats.get('message_logs', 0))}",
        f"• unanswered questions: {human_count(stats.get('unanswered_questions', 0))}",
        f"• pending (open) questions: {human_count(stats.get('open_questions', 0))}",
        f"• cache entries: {human_count(stats.get('cache_entries', 0))}",
        f"• latest memory: {human_time(stats.get('latest_memory'))}",
        "",
        f"• AI calls 24h: {usage.get('calls', 0)} (cache {usage.get('cached_calls', 0)}, "
        f"failed {usage.get('failed_calls', 0)})",
        f"• tokens 24h: {human_count(usage.get('tokens', 0))} | "
        f"avg latency: {usage.get('avg_latency_ms', 0)} ms",
        f"• database: {'✅' if db_health.get('available') else '⛔'} "
        f"{db_health.get('latency_ms', '—')} ms",
        f"• model: <code>{services.settings.groq_model}</code>",
    ]
    if stats.get("error"):
        lines.append(f"⚠️ {stats['error']}")
    await _safe_send(update.effective_message, "\n".join(lines), allow_parse_mode=True)


async def cmd_memory_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    services = services_of(context)
    stats = await asyncio.to_thread(services.db.get_stats)
    topics = stats.get("topics") or []
    top_questions = []
    try:
        top_questions = await asyncio.to_thread(services.db.recent_unanswered_questions, 5)
    except Exception:
        pass
    lines = [
        "🧠 <b>Memory analytics</b>",
        f"• latest memory: {human_time(stats.get('latest_memory'))}",
        f"• memory generation: {stats.get('memory_generation', 0)} (cache key version)",
    ]
    if topics:
        lines.append("\n<b>Topic breakdown (Q&A):</b>")
        for row in topics:
            lines.append(f"• {row.get('topic')}: {row.get('total')}")
    if top_questions:
        lines.append("\n<b>Admin-এর উত্তর পায়নি এমন প্রশ্ন:</b>")
        for row in top_questions:
            lines.append(f"• {human_time(row.get('message_timestamp'))} — "
                         f"{truncate(row.get('message_text') or '', 90)}")
    lines.append("\n<i>এগুলো AI memory হিসেবে ব্যবহার হয় না (unanswered)।</i>")
    await _safe_send(update.effective_message, "\n".join(lines), allow_parse_mode=True)


async def cmd_adminhelp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    services = services_of(context)
    flags = {key: services.runtime_flag(key, True) for key in TOGGLE_KEYS}
    flag_lines = [f"• {key}: {'on' if value else 'off'}" for key, value in flags.items()]
    text = (
        "🛠 <b>Admin commands</b>\n"
        "/stats — সব statistics\n"
        "/memory_stats — memory analytics (topic, unanswered প্রশ্ন)\n"
        "/memory [প্রশ্ন] — retrieval debug\n"
        "/clear_memory — memory মুছে ফেলা (confirmation লাগবে)\n"
        "/set &lt;key&gt; &lt;on|off&gt; — নিচের switch বদলানো\n"
        "/reload — .env আবার পড়ে settings refresh\n"
        "/whoami — তোমার id\n\n"
        "<b>Runtime switches:</b>\n" + "\n".join(flag_lines) +
        "\n\n<b>clear_memory scope:</b> qa | admin | logs | cache | all"
    )
    await _safe_send(update.effective_message, text, allow_parse_mode=True)


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    services = services_of(context)
    args = context.args or []
    if len(args) < 2 or args[0] not in TOGGLE_KEYS:
        keys = ", ".join(TOGGLE_KEYS)
        await _safe_send(
            update.effective_message,
            f"ব্যবহার: /set &lt;key&gt; &lt;on|off&gt;\nkey: {keys}",
            allow_parse_mode=True,
        )
        return
    key, raw_value = args[0], args[1].strip().lower()
    if raw_value in {"on", "true", "1", "yes", "enable"}:
        value = True
    elif raw_value in {"off", "false", "0", "no", "disable"}:
        value = False
    else:
        await _safe_send(update.effective_message, "value হবে on অথবা off।")
        return
    ok = await asyncio.to_thread(
        services.set_runtime_flag, key, value, updated_by=getattr(update.effective_user, "id", None)
    )
    await _safe_send(update.effective_message,
                     f"{'✅' if ok else '⚠️'} {key} = {'on' if value else 'off'}")


async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    from config import get_settings as _get_settings
    try:
        new_settings = await asyncio.to_thread(_get_settings, reload=True)
        new_services = await asyncio.to_thread(build_services, new_settings, force=True)
        context.bot_data["services"] = new_services
        summary = new_settings.public_summary()
        problems = summary.get("problems") or []
        warnings = summary.get("warnings") or []
        text = (
            "🔄 settings reloaded\n"
            f"model: <code>{summary['groq_model']}</code>\n"
            f"AI enabled: {summary['groq_enabled']}\n"
            f"database: {'✅' if summary['database_configured'] else '⛔'}\n"
            f"target admins: {summary['target_admins'] or '—'}\n"
        )
        if problems:
            text += f"\n⛔ problems: {', '.join(problems)}"
        if warnings:
            text += f"\n⚠️ warnings: {', '.join(warnings)}"
        text += ("\n\n<i>Render-এ env variable বদলালে service restart লাগবে; "
                 "লোকালি .env edit করে /reload দিলেই চলবে।</i>")
        await _safe_send(update.effective_message, text, allow_parse_mode=True)
    except Exception as exc:
        logger.error("reload failed: %s", exc)
        await _safe_send(update.effective_message, f"⚠️ reload ব্যর্থ: {exc}")


async def cmd_clear_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    services = services_of(context)
    args = [a.lower() for a in (context.args or [])]
    scope = next((a for a in args if a in {"qa", "admin", "logs", "cache", "all"}), "")
    confirmed = "confirm" in args
    chat_id = getattr(update.effective_chat, "id", None)

    if not scope:
        await _safe_send(
            update.effective_message,
            "⚠️ ব্যবহার: <code>/clear_memory &lt;scope&gt; confirm</code>\n\n"
            "<b>scope:</b>\n"
            "• <code>qa</code> — প্রশ্ন-উত্তর memory + pending প্রশ্ন\n"
            "• <code>admin</code> — Admin messages (+ analytics log)\n"
            "• <code>logs</code> — শুধু analytics log\n"
            "• <code>cache</code> — cached উত্তর\n"
            "• <code>all</code> — সবকিছু (admin+qa+logs+cache)\n\n"
            "⚠️ এই কাজটি ফেরানো যায় না।",
            allow_parse_mode=True,
        )
        return

    if not confirmed:
        stats = await asyncio.to_thread(services.db.get_stats)
        preview = (
            f"⚠️ <b>Confirmation দরকার</b>\n\n"
            f"scope <code>{scope}</code> মুছে ফেলবে:\n"
            f"• admin messages: {human_count(stats.get('admin_messages', 0))}\n"
            f"• Q&A memory: {human_count(stats.get('qa_pairs', 0))}\n"
            f"• logged messages: {human_count(stats.get('message_logs', 0))}\n"
            f"• cached answers: {human_count(stats.get('cache_entries', 0))}\n\n"
            f"নিশ্চিত হলে পাঠাও:\n<code>/clear_memory {scope} confirm</code>"
        )
        await _safe_send(update.effective_message, preview, allow_parse_mode=True)
        return

    try:
        deleted = await asyncio.to_thread(services.db.clear_memory, scope, chat_id=chat_id)
    except Exception as exc:
        logger.error("clear_memory failed: %s", exc)
        await _safe_send(update.effective_message, f"⚠️ ব্যর্থ: {exc}")
        return
    if services.cache:
        services.cache.invalidate_chat(chat_id)
    logger.warning("admin %s cleared memory scope=%s deleted=%s",
                   getattr(update.effective_user, "id", None), scope, deleted)
    detail = "\n".join(f"• {table}: {count}" for table, count in deleted.items() if count)
    await _safe_send(
        update.effective_message,
        f"🧹 scope <code>{scope}</code> clear করা হয়েছে ✅\n{detail or '• কোনো row ছিল না'}\n\n"
        "<i>নতুন memory আবার Admin-এর message থেকে জমা হবে।</i>",
        allow_parse_mode=True,
    )


async def cmd_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    services = services_of(context)
    result = await asyncio.to_thread(run_maintenance, services, True)
    await _safe_send(update.effective_message,
                     f"🧽 maintenance: {result or 'skipped'}")


# --------------------------------------------------------------------------- #
# message handlers
# --------------------------------------------------------------------------- #
async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_of(context)
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None:
        return
    if _is_duplicate(update, services):
        logger.debug("duplicate update skipped (group): %s", update.update_id)
        return
    if getattr(user, "is_bot", False):
        return
    if not services.permissions.in_target_group(chat.id):
        logger.debug("message from non-target group ignored: %s", chat.id)
        return

    is_target_admin = services.permissions.is_target_admin(user.id)
    memory_enabled = services.runtime_flag("memory_enabled", True)
    tracking = []

    if memory_enabled:
        try:
            if is_target_admin:
                tracking.append(await asyncio.to_thread(
                    services.tracker.track_admin_message, message, user))
            else:
                tracking.append(await asyncio.to_thread(
                    services.tracker.track_member_message, message, user))
        except Exception as exc:
            logger.error("tracking failed: %s", exc)
    if tracking:
        logger.debug("tracked: %s", tracking)

    # decide whether to answer
    auto_reply = services.runtime_flag("auto_reply_enabled", True)
    if not auto_reply and not is_target_admin:
        return
    should_answer, reason = services.permissions.is_member_question(
        message, user, bot_id=getattr(context.bot, "id", None),
        bot_username=getattr(context.bot, "username", None),
    )
    if not should_answer:
        return
    await _answer_question(update, context,
                           message.text or message.caption or "",
                           source=f"group:{reason}")


async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = services_of(context)
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if _is_duplicate(update, services):
        logger.debug("duplicate update skipped (private): %s", update.update_id)
        return
    if getattr(user, "is_bot", False):
        return
    if not services.settings.answer_in_private_chat:
        await _safe_send(message, "ℹ️ এই bot শুধু গ্রুপে প্রশ্নের উত্তর দেয়।")
        return
    text = message.text or message.caption or ""
    if not text.strip():
        return
    # in private chat the group memory of the configured GROUP_ID is used
    try:
        await asyncio.to_thread(
            services.db.upsert_bot_user, user,
            is_target_admin=services.permissions.is_target_admin(user.id),
        )
    except Exception:
        pass
    cleaned = clean_question(text, getattr(context.bot, "username", None),
                             limit=services.settings.max_question_chars)
    await _answer_question(update, context, cleaned, source="private")


async def on_non_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Track non-text messages (photos, voice notes) without answering."""
    services = services_of(context)
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or getattr(user, "is_bot", False):
        return
    chat = update.effective_chat
    if chat is None or not services.permissions.in_target_group(chat.id):
        return
    if not services.runtime_flag("memory_enabled", True):
        return
    caption = message.caption or ""
    if not caption:
        return
    try:
        if services.permissions.is_target_admin(user.id):
            await asyncio.to_thread(services.tracker.track_admin_message, message, user)
        else:
            await asyncio.to_thread(services.tracker.track_member_message, message, user)
    except Exception as exc:
        logger.debug("non-text tracking failed: %s", exc)


# --------------------------------------------------------------------------- #
# error handling
# --------------------------------------------------------------------------- #
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all error handler: log (redacted) and inform the user politely."""
    error = getattr(context, "error", None)
    update_id = getattr(update, "update_id", None) if update else None
    logger.error("Telegram handler error (update=%s): %s: %s",
                 update_id, type(error).__name__ if error else "?", error)
    if isinstance(error, (Forbidden, BadRequest)):
        logger.warning("non-retryable Telegram error: %s", error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ সাময়িক সমস্যা হয়েছে। আবার চেষ্টা করো।"
            )
        except TelegramError:
            pass


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #
def register_handlers(application: Application, services: Services) -> None:
    """Attach every handler to ``application`` (idempotent)."""
    application.bot_data["services"] = services

    # group 0: commands
    application.add_handler(CommandHandler("start", cmd_start, block=False))
    application.add_handler(CommandHandler("help", cmd_help, block=False))
    application.add_handler(CommandHandler(["ask", "a"], cmd_ask, block=False))
    application.add_handler(CommandHandler("ping", cmd_ping, block=False))
    application.add_handler(CommandHandler("whoami", cmd_whoami, block=False))
    application.add_handler(CommandHandler("status", cmd_status, block=False))
    application.add_handler(CommandHandler("memory", cmd_memory, block=False))

    # group 1: admin-only commands
    application.add_handler(CommandHandler("stats", cmd_stats, block=False))
    application.add_handler(CommandHandler("memory_stats", cmd_memory_stats, block=False))
    application.add_handler(CommandHandler("clear_memory", cmd_clear_memory, block=False))
    application.add_handler(CommandHandler("set", cmd_set, block=False))
    application.add_handler(CommandHandler("reload", cmd_reload, block=False))
    application.add_handler(CommandHandler("maintenance", cmd_maintenance, block=False))
    application.add_handler(CommandHandler("adminhelp", cmd_adminhelp, block=False))

    # group 2: private text
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        on_private_message, block=False), group=2)

    # group 3: group/supergroup text
    application.add_handler(MessageHandler(
        (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP) & filters.TEXT & ~filters.COMMAND,
        on_group_message, block=False), group=3)

    # group 4: captions (photos/voice with caption) -- tracking only
    application.add_handler(MessageHandler(
        (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP) & filters.CAPTION,
        on_non_text_message, block=False), group=4)

    application.add_error_handler(on_error)
    logger.info("handlers registered: %s public + %s admin commands",
                len(PUBLIC_COMMANDS), len(ADMIN_COMMANDS))


async def _post_init(application: Application) -> None:
    """Runs once the bot is initialised (both polling and webhook modes)."""
    services: Services = application.bot_data.get("services")
    if services is None:
        return
    me = await application.bot.get_me()
    application.bot_data["bot_username"] = me.username
    application.bot_data["bot_id"] = me.id
    logger.info("bot initialised: @%s (id=%s)", me.username, me.id)

    summary = await asyncio.to_thread(startup_tasks, services)
    logger.info("startup tasks: %s", summary)

    try:
        await application.bot.set_my_commands(
            [BotCommand(name, desc) for name, desc in (
                ("start", "Bot চালু ও পরিচিতি"),
                ("ask", "Admin-এর তথ্যের ভিত্তিতে প্রশ্নের উত্তর"),
                ("help", "সাহায্য"),
                ("memory", "আমি কী মনে রেখেছি"),
                ("status", "Bot status"),
                ("whoami", "তোমার Telegram id"),
                ("ping", "Bot alive কিনা পরীক্ষা"),
            )],
            scope=BotCommandScopeAllPrivateChats(),
        )
        await application.bot.set_my_commands(
            [BotCommand("start", "Bot চালু ও পরিচিতি"),
             BotCommand("ask", "প্রশ্ন করো: /ask BTC কেমন?"),
             BotCommand("memory", "memory সারসংক্ষেপ"),
             BotCommand("help", "সাহায্য"),
             BotCommand("ping", "Bot alive কিনা পরীক্ষা")],
            scope=BotCommandScopeAllGroupChats(),
        )
    except TelegramError as exc:
        logger.debug("set_my_commands failed: %s", exc)

    if services.settings.set_webhook_on_startup and services.settings.webhook_url:
        try:
            await application.bot.set_webhook(
                url=services.settings.webhook_url,
                secret_token=services.settings.webhook_secret or None,
                allowed_updates=ALLOWED_UPDATES,
                drop_pending_updates=services.settings.drop_pending_updates,
                max_connections=services.settings.webhook_max_connections,
            )
            info = await application.bot.get_webhook_info()
            logger.info("webhook set: url=%s pending=%s last_error=%s",
                        info.url, info.pending_update_count, info.last_error_message)
        except TelegramError as exc:
            services.errors.append(f"webhook setup failed: {exc}")
            logger.error("could not set webhook: %s", exc)
    else:
        logger.info("webhook auto-setup skipped (PUBLIC_URL/SET_WEBHOOK_ON_STARTUP)")


async def _post_shutdown(application: Application) -> None:
    services: Services = application.bot_data.get("services") or None
    logger.info("application shutting down")
    if services is not None:
        try:
            await asyncio.to_thread(services.db.close_pool)
        except Exception:
            pass


def build_application(settings: Settings | None = None,
                      services: Services | None = None,
                      *, for_webhook: bool = False) -> tuple[Application, Services]:
    """Create the PTB ``Application`` with every handler registered."""
    settings = settings or get_settings()
    services = services or build_services(settings)

    builder = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .connect_timeout(10.0)
        .read_timeout(10.0)
        .write_timeout(10.0)
        .pool_timeout(10.0)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .concurrent_updates(False)
    )
    if for_webhook:
        builder = builder.updater(None)  # webhook mode: we drive updates ourselves
    application = builder.build()
    register_handlers(application, services)
    return application, services


def command_list() -> dict[str, Sequence[str]]:
    return {"public": PUBLIC_COMMANDS, "admin": ADMIN_COMMANDS}

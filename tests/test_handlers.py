"""Command handler tests: permissions, admin commands, answer flow."""

from __future__ import annotations

import asyncio

import pytest

from ai.caching import AnswerCache
from ai.groq_client import AIResult
from ai.responder import AnswerService
from ai.retrieval import RetrievalEngine
from bot_telegram import handlers
from bot_telegram.message_tracker import MessageTracker
from bot_telegram.permissions import Permissions
from config import load_settings
from services import Services
from tests.fakes import FakeBot, FakeChat, FakeDB, make_message, make_user
from utils.helpers import RateLimiter

CHAT_ID = -1001234567890
ADMIN_ID = 777000111
MEMBER_ID = 555000222


class FakeGroq:
    enabled = True
    last_error = ""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return AIResult(ok=True, text="ঠিক আছে, ধৈর্য ধরো।", model="openai/gpt-oss-120b",
                        latency_ms=100, total_tokens=50)

    def health_check(self, **kwargs):
        return {"ok": True, "error_kind": "none"}


class FakeUpdate:
    def __init__(self, user, chat, message, update_id: int = 1):
        self.effective_user = user
        self.effective_chat = chat
        self.effective_message = message
        self.update_id = update_id
        self.callback_query = None


class FakeContext:
    def __init__(self, services, bot: FakeBot, args: list[str] | None = None):
        self.bot_data = {"services": services}
        self.bot = bot
        self.args = args or []


@pytest.fixture()
def settings():
    return load_settings({
        "BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
        "GROQ_API_KEY": "gsk_test",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "TARGET_ADMIN_ID": str(ADMIN_ID),
        "ADMIN_ID": str(ADMIN_ID),
        "GROUP_ID": str(CHAT_ID),
        "PUBLIC_URL": "https://example.com",
        "WEBHOOK_SECRET": "s" * 32,
    })


@pytest.fixture()
def stack(settings):
    db = FakeDB()
    groq = FakeGroq()
    retriever = RetrievalEngine(db, settings)
    cache = AnswerCache(settings, db)
    tracker = MessageTracker(db, settings)
    return Services(
        settings=settings, db=db, permissions=Permissions(settings),
        tracker=tracker, retriever=retriever, groq=groq, cache=cache,
        responder=AnswerService(settings, db, retriever, groq, cache),
        rate_limiter=RateLimiter(10, 60), ready=True,
    )


def make_group_update(user, text: str, message_id: int = 1, update_id: int = 1,
                      reply_to=None):
    message = make_message(CHAT_ID, message_id, user, text, reply_to=reply_to)
    return FakeUpdate(user, message.chat, message, update_id=update_id)


class TestPublicCommands:
    def test_start_private(self, stack):
        user = make_user(MEMBER_ID, "member")
        update = FakeUpdate(user, FakeChat(MEMBER_ID, "private"),
                            make_message(MEMBER_ID, 1, user, "/start", chat_type="private"))
        asyncio.run(handlers.cmd_start(update, FakeContext(stack, FakeBot())))
        assert update.effective_message.replies
        assert "INFO GROUP" in update.effective_message.replies[0]

    def test_help_mentions_admin_commands_only_for_admins(self, stack):
        member = make_user(MEMBER_ID, "member")
        update = make_group_update(member, "/help")
        asyncio.run(handlers.cmd_help(update, FakeContext(stack, FakeBot())))
        member_text = update.effective_message.replies[0]
        assert "/ask" in member_text and "/stats" not in member_text

    def test_ping(self, stack):
        user = make_user(MEMBER_ID)
        update = make_group_update(user, "/ping")
        asyncio.run(handlers.cmd_ping(update, FakeContext(stack, FakeBot())))
        assert "পং" in update.effective_message.replies[0]

    def test_whoami_shows_ids(self, stack):
        user = make_user(MEMBER_ID, "member")
        update = make_group_update(user, "/whoami")
        asyncio.run(handlers.cmd_whoami(update, FakeContext(stack, FakeBot())))
        text = update.effective_message.replies[0]
        assert str(MEMBER_ID) in text and str(CHAT_ID) in text

    def test_status_works_for_member_and_admin(self, stack):
        member = make_user(MEMBER_ID)
        update = make_group_update(member, "/status")
        asyncio.run(handlers.cmd_status(update, FakeContext(stack, FakeBot())))
        assert "running" in update.effective_message.replies[0]

        admin = make_user(ADMIN_ID, "target_admin")
        admin_update = make_group_update(admin, "/status", message_id=2)
        asyncio.run(handlers.cmd_status(admin_update, FakeContext(stack, FakeBot())))
        assert "Admin detail" in admin_update.effective_message.replies[0]


class TestAdminCommandPermissions:
    def test_member_denied_on_stats(self, stack):
        member = make_user(MEMBER_ID)
        update = make_group_update(member, "/stats")
        asyncio.run(handlers.cmd_stats(update, FakeContext(stack, FakeBot())))
        assert "⛔" in update.effective_message.replies[0]

    def test_admin_allowed_on_stats(self, stack):
        admin = make_user(ADMIN_ID, "target_admin")
        update = make_group_update(admin, "/stats")
        asyncio.run(handlers.cmd_stats(update, FakeContext(stack, FakeBot())))
        assert "Statistics" in update.effective_message.replies[0]

    def test_member_denied_on_clear_memory(self, stack):
        member = make_user(MEMBER_ID)
        update = make_group_update(member, "/clear_memory all confirm")
        asyncio.run(handlers.cmd_clear_memory(update, FakeContext(stack, FakeBot(),
                                                                args=["all", "confirm"])))
        assert "⛔" in update.effective_message.replies[0]
        assert stack.db.clear_calls == [], "a member must not be able to clear memory"

    def test_member_denied_on_set(self, stack):
        member = make_user(MEMBER_ID)
        update = make_group_update(member, "/set ai_enabled off")
        asyncio.run(handlers.cmd_set(update, FakeContext(stack, FakeBot(),
                                                        args=["ai_enabled", "off"])))
        assert "⛔" in update.effective_message.replies[0]
        assert stack.db.settings_map == {}


class TestClearMemoryConfirmation:
    def test_preview_without_confirm(self, stack):
        admin = make_user(ADMIN_ID, "target_admin")
        update = make_group_update(admin, "/clear_memory all")
        asyncio.run(handlers.cmd_clear_memory(update, FakeContext(stack, FakeBot(),
                                                                 args=["all"])))
        text = update.effective_message.replies[0]
        assert "Confirmation" in text and "confirm" in text

    def test_usage_without_scope(self, stack):
        admin = make_user(ADMIN_ID, "target_admin")
        update = make_group_update(admin, "/clear_memory")
        asyncio.run(handlers.cmd_clear_memory(update, FakeContext(stack, FakeBot(), args=[])))
        assert "scope" in update.effective_message.replies[0]

    def test_confirmed_clear_runs(self, stack):
        admin = make_user(ADMIN_ID, "target_admin")
        update = make_group_update(admin, "/clear_memory qa confirm")
        asyncio.run(handlers.cmd_clear_memory(update, FakeContext(stack, FakeBot(),
                                                                 args=["qa", "confirm"])))
        assert "clear করা হয়েছে" in update.effective_message.replies[0]


class TestSetCommand:
    def test_set_toggle(self, stack):
        admin = make_user(ADMIN_ID, "target_admin")
        update = make_group_update(admin, "/set ai_enabled off")
        asyncio.run(handlers.cmd_set(update, FakeContext(stack, FakeBot(),
                                                        args=["ai_enabled", "off"])))
        assert stack.db.settings_map["ai_enabled"] == "false"
        assert "off" in update.effective_message.replies[0]

    def test_unknown_key(self, stack):
        admin = make_user(ADMIN_ID, "target_admin")
        update = make_group_update(admin, "/set nope on")
        asyncio.run(handlers.cmd_set(update, FakeContext(stack, FakeBot(), args=["nope", "on"])))
        assert "ব্যবহার" in update.effective_message.replies[0]


class TestAnswerFlow:
    def test_ask_command_answers(self, stack):
        member = make_user(MEMBER_ID, "member")
        update = make_group_update(member, "/ask BTC এখন কেমন?")
        asyncio.run(handlers.cmd_ask(update, FakeContext(stack, FakeBot(),
                                                        args=["BTC", "এখন", "কেমন?"])))
        assert update.effective_message.replies
        assert "ধৈর্য" in update.effective_message.replies[0]
        assert stack.groq.calls == 1

    def test_ask_without_question_shows_usage(self, stack):
        member = make_user(MEMBER_ID)
        update = make_group_update(member, "/ask")
        asyncio.run(handlers.cmd_ask(update, FakeContext(stack, FakeBot(), args=[])))
        assert "ব্যবহার" in update.effective_message.replies[0]

    def test_ai_disabled_switch_blocks_answering(self, stack):
        stack.db.settings_map["ai_enabled"] = "false"
        member = make_user(MEMBER_ID)
        update = make_group_update(member, "/ask BTC?")
        asyncio.run(handlers.cmd_ask(update, FakeContext(stack, FakeBot(), args=["BTC?"])))
        assert "বন্ধ" in update.effective_message.replies[0]
        assert stack.groq.calls == 0

    def test_rate_limit_applies(self, stack):
        stack.rate_limiter = RateLimiter(limit=1, window_seconds=60)
        member = make_user(MEMBER_ID)
        first = make_group_update(member, "/ask BTC?", message_id=1)
        asyncio.run(handlers.cmd_ask(first, FakeContext(stack, FakeBot(), args=["BTC?"])))
        second = make_group_update(member, "/ask ETH?", message_id=2, update_id=2)
        asyncio.run(handlers.cmd_ask(second, FakeContext(stack, FakeBot(), args=["ETH?"])))
        assert "ধীরে" in second.effective_message.replies[0]

    def test_group_message_without_trigger_is_silent(self, stack):
        member = make_user(MEMBER_ID, "member")
        update = make_group_update(member, "আজ বাজারে কী হচ্ছে?")
        asyncio.run(handlers.on_group_message(update, FakeContext(stack, FakeBot())))
        assert update.effective_message.replies == []
        assert stack.groq.calls == 0

    def test_mention_triggers_answer(self, stack):
        member = make_user(MEMBER_ID, "member")
        update = make_group_update(member, "@InfoGroupAIBot BTC এখন কেমন?")
        asyncio.run(handlers.on_group_message(update, FakeContext(stack, FakeBot())))
        assert update.effective_message.replies

    def test_duplicate_update_is_ignored(self, stack):
        member = make_user(MEMBER_ID, "member")
        update = make_group_update(member, "@InfoGroupAIBot BTC?", update_id=4242)
        context = FakeContext(stack, FakeBot())
        asyncio.run(handlers.on_group_message(update, context))
        replies_after_first = len(update.effective_message.replies)
        asyncio.run(handlers.on_group_message(update, context))
        assert len(update.effective_message.replies) == replies_after_first

    def test_private_message_is_answered(self, stack):
        user = make_user(MEMBER_ID, "member")
        message = make_message(MEMBER_ID, 1, user, "BTC এখন কেমন?", chat_type="private")
        update = FakeUpdate(user, message.chat, message)
        asyncio.run(handlers.on_private_message(update, FakeContext(stack, FakeBot())))
        assert message.replies

    def test_private_message_disabled_by_config(self, settings):
        settings = load_settings({
            "BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
            "DATABASE_URL": "postgresql://u:p@localhost/db",
            "TARGET_ADMIN_ID": str(ADMIN_ID),
            "ANSWER_IN_PRIVATE_CHAT": "false",
        })
        db = FakeDB()
        retriever = RetrievalEngine(db, settings)
        services = Services(settings=settings, db=db, permissions=Permissions(settings),
                            retriever=retriever, groq=FakeGroq(),
                            cache=AnswerCache(settings, db),
                            responder=AnswerService(settings, db, retriever, FakeGroq()),
                            rate_limiter=RateLimiter(5, 60))
        user = make_user(MEMBER_ID)
        message = make_message(MEMBER_ID, 1, user, "hi", chat_type="private")
        update = FakeUpdate(user, message.chat, message)
        asyncio.run(handlers.on_private_message(update, FakeContext(services, FakeBot())))
        assert "শুধু গ্রুপে" in message.replies[0]

    def test_handler_survives_ai_crash(self, stack):
        class Exploding:
            enabled = True
            last_error = ""

            def chat(self, messages, **kwargs):
                raise RuntimeError("boom")

            def health_check(self, **kwargs):
                return {"ok": False}

        stack.responder.groq = Exploding()
        member = make_user(MEMBER_ID)
        update = make_group_update(member, "/ask BTC?")
        asyncio.run(handlers.cmd_ask(update, FakeContext(stack, FakeBot(), args=["BTC?"])))
        # the responder swallows the client error and still answers
        assert update.effective_message.replies


def test_register_handlers_wires_commands(stack):
    from telegram.ext import Application

    application = Application.builder().token("123456789:AA-dummy-token-for-tests-only-000000").build()
    handlers.register_handlers(application, stack)
    commands = set()
    for group in application.handlers.values():
        for handler in group:
            for command in getattr(handler, "commands", []) or []:
                commands.add(command)
    for expected in handlers.PUBLIC_COMMANDS + handlers.ADMIN_COMMANDS:
        assert expected in commands, f"/{expected} is not registered"
    assert application.error_handlers
    assert application.bot_data["services"] is stack

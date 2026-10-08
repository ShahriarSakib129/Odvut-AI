"""Permission system and trigger detection tests."""

from __future__ import annotations

import pytest

from bot_telegram.permissions import Permissions, clean_question, split_command_args
from config import load_settings
from tests.fakes import make_message, make_user

GROUP_ID = -1001234567890
ADMIN_ID = 777000111
MEMBER_ID = 555000222
OTHER_ADMIN_ID = 888000333


@pytest.fixture()
def settings():
    return load_settings({
        "BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
        "GROQ_API_KEY": "gsk_test",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "TARGET_ADMIN_ID": str(ADMIN_ID),
        "ADMIN_ID": str(ADMIN_ID),
        "GROUP_ID": str(GROUP_ID),
        "PUBLIC_URL": "https://example.com",
        "WEBHOOK_SECRET": "s" * 32,
    })


@pytest.fixture()
def perms(settings):
    return Permissions(settings)


class TestRoles:
    def test_target_admin_recognised(self, perms):
        assert perms.is_target_admin(ADMIN_ID) is True
        assert perms.is_target_admin(MEMBER_ID) is False

    def test_target_admin_is_bot_admin_by_default(self, perms):
        assert perms.is_bot_admin(ADMIN_ID) is True
        assert perms.is_bot_admin(MEMBER_ID) is False
        assert perms.is_bot_admin(None) is False

    def test_target_group_only(self, perms):
        assert perms.in_target_group(GROUP_ID) is True
        assert perms.in_target_group(-999) is False

    def test_any_group_when_group_id_unset(self):
        settings = load_settings({
            "BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
            "DATABASE_URL": "postgresql://u:p@localhost/db",
            "TARGET_ADMIN_ID": str(ADMIN_ID),
        })
        assert Permissions(settings).in_target_group(-555) is True

    def test_group_admin_requires_opt_in(self, perms):
        perms._group_admins[GROUP_ID] = {OTHER_ADMIN_ID}
        actor = type("A", (), {"is_bot_admin": False, "chat_type": "supergroup",
                               "chat_id": GROUP_ID, "is_group_admin": True,
                               "user_id": OTHER_ADMIN_ID})()
        allowed, reason = perms.authorize_admin(actor)
        assert allowed is False and reason == "not_authorized"

    def test_group_admin_allowed_when_enabled(self):
        settings = load_settings({
            "BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
            "DATABASE_URL": "postgresql://u:p@localhost/db",
            "TARGET_ADMIN_ID": str(ADMIN_ID),
            "GROUP_ID": str(GROUP_ID),
            "ALLOW_GROUP_ADMINS": "true",
        })
        perms = Permissions(settings)
        perms._group_admins[GROUP_ID] = {OTHER_ADMIN_ID}
        actor = type("A", (), {"is_bot_admin": False, "chat_type": "supergroup",
                               "chat_id": GROUP_ID, "is_group_admin": True,
                               "user_id": OTHER_ADMIN_ID})()
        allowed, reason = perms.authorize_admin(actor)
        assert allowed is True and reason == "group_admin"


class TestTriggers:
    def test_mention_triggers(self, perms):
        message = make_message(GROUP_ID, 1, make_user(MEMBER_ID), "@InfoGroupAIBot BTC kemon?")
        should, reason = perms.is_member_question(message, message.from_user,
                                                  bot_id=999, bot_username="InfoGroupAIBot")
        assert should is True and reason == "mention"

    def test_reply_to_bot_triggers(self, perms):
        bot_message = make_message(GROUP_ID, 2, make_user(999, "InfoGroupAIBot", is_bot=True),
                                   "উত্তর")
        message = make_message(GROUP_ID, 3, make_user(MEMBER_ID), "আর একবার বলো",
                               reply_to=bot_message)
        should, reason = perms.is_member_question(message, message.from_user,
                                                  bot_id=999, bot_username="InfoGroupAIBot")
        assert should is True and reason == "reply_to_bot"

    def test_plain_message_does_not_trigger(self, perms):
        message = make_message(GROUP_ID, 4, make_user(MEMBER_ID), "BTC aaj bhalo chilo")
        should, reason = perms.is_member_question(message, message.from_user,
                                                  bot_id=999, bot_username="InfoGroupAIBot")
        assert should is False and reason == "no_trigger"

    def test_target_admin_never_triggers(self, perms):
        message = make_message(GROUP_ID, 5, make_user(ADMIN_ID), "@InfoGroupAIBot ki obostha?")
        should, reason = perms.is_member_question(message, message.from_user,
                                                  bot_id=999, bot_username="InfoGroupAIBot")
        assert should is False and reason == "from_target_admin"

    def test_other_group_ignored(self, perms):
        message = make_message(-999, 6, make_user(MEMBER_ID), "@InfoGroupAIBot hi")
        should, reason = perms.is_member_question(message, message.from_user,
                                                  bot_id=999, bot_username="InfoGroupAIBot")
        assert should is False and reason == "other_group"

    def test_bot_messages_ignored(self, perms):
        message = make_message(GROUP_ID, 7, make_user(999, "bot", is_bot=True), "@bot hi")
        should, reason = perms.is_member_question(message, message.from_user,
                                                  bot_id=999, bot_username="InfoGroupAIBot")
        assert should is False and reason == "from_bot"

    def test_keyword_trigger(self):
        settings = load_settings({
            "BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
            "DATABASE_URL": "postgresql://u:p@localhost/db",
            "TARGET_ADMIN_ID": str(ADMIN_ID),
            "TRIGGER_KEYWORDS": "askai",
        })
        perms = Permissions(settings)
        message = make_message(GROUP_ID, 8, make_user(MEMBER_ID), "askai BTC kemon?")
        should, reason = perms.is_member_question(message, message.from_user,
                                                  bot_id=999, bot_username="InfoGroupAIBot")
        assert should is True and reason.startswith("keyword")


class TestQuestionCleaning:
    def test_mention_removed(self):
        assert clean_question("@InfoGroupAIBot BTC কেমন?", "InfoGroupAIBot") == "BTC কেমন?"

    def test_command_args(self):
        assert split_command_args("/ask BTC entry?", "ask") == "BTC entry?"
        assert split_command_args("/ask", "ask") == ""
        assert split_command_args("plain text", "") == "plain text"

    def test_length_clamped(self):
        assert len(clean_question("x" * 500, None, limit=50)) <= 51

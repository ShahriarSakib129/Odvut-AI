"""Admin memory + Q&A pairing tests (these exercise real SQL)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot_telegram.message_tracker import MessageTracker
from tests.fakes import make_message, make_user

ADMIN_ID = 777000111
MEMBER_ID = 555000222
CHAT_ID = -1001234567890


@pytest.fixture()
def tracker(settings, clean_db):
    from ai.embeddings import embed_text

    return MessageTracker(clean_db, settings, embedder=embed_text)


def admin_message(text: str, message_id: int, reply_to=None, minutes_ago: int = 0):
    return make_message(
        CHAT_ID, message_id, make_user(ADMIN_ID, "target_admin"), text,
        reply_to=reply_to,
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


def member_message(text: str, message_id: int, minutes_ago: int = 0):
    return make_message(
        CHAT_ID, message_id, make_user(MEMBER_ID, "member_one"), text,
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


class TestAdminMemory:
    def test_admin_message_is_stored(self, tracker, clean_db):
        message = admin_message("আজ মার্কেট range bound, ধৈর্য ধরুন", 101)
        result = tracker.track_admin_message(message, message.from_user)
        assert result["stored"] is True
        assert clean_db.count_admin_messages(CHAT_ID) == 1
        row = clean_db.recent_admin_messages(CHAT_ID, 1)[0]
        assert row["topic"] in {"market_structure", "general", "risk_management"}
        assert row["keywords"]
        assert row["embedding"]

    def test_duplicate_message_is_idempotent(self, tracker, clean_db):
        message = admin_message("একই message", 102)
        tracker.track_admin_message(message, message.from_user)
        tracker.track_admin_message(message, message.from_user)
        assert clean_db.count_admin_messages(CHAT_ID) == 1

    def test_empty_text_skipped(self, tracker, clean_db):
        message = admin_message("", 103)
        assert tracker.track_admin_message(message, message.from_user)["stored"] is False
        assert clean_db.count_admin_messages(CHAT_ID) == 0


class TestMemberQuestionTracking:
    def test_question_creates_pending_entry(self, tracker, clean_db):
        message = member_message("BTC এখন entry নেওয়া যাবে?", 201)
        result = tracker.track_member_message(message, message.from_user)
        assert result["question"] is True
        assert result["pending_id"] is not None
        assert clean_db.count_open_questions(CHAT_ID) == 1

    def test_statement_is_logged_but_not_a_question(self, tracker, clean_db):
        message = member_message("আজ বাজারে ভলিউম কম", 202)
        result = tracker.track_member_message(message, message.from_user)
        assert result["question"] is False
        assert result["pending_id"] is None
        assert clean_db.count_message_logs() == 1
        assert clean_db.count_qa_pairs() == 0


class TestPairingByReply:
    def test_admin_reply_creates_qa_memory(self, tracker, clean_db):
        question = member_message("ETH এখন কেমন?", 301)
        tracker.track_member_message(question, question.from_user)

        answer = admin_message("support zone এর দিকে আছে, confirmation pending", 302,
                               reply_to=question)
        result = tracker.track_admin_message(answer, answer.from_user)

        assert result["qa_id"] is not None
        assert result["pair_method"] == "reply"
        pairs = clean_db.recent_qa_pairs(CHAT_ID, 5)
        assert len(pairs) == 1
        assert "ETH" in pairs[0]["question_text"]
        assert "support" in pairs[0]["admin_answer_text"]
        assert pairs[0]["pair_method"] == "reply"
        assert pairs[0]["pair_confidence"] >= 0.9
        # the pending question is closed and the analytics log updated
        assert clean_db.count_open_questions(CHAT_ID) == 0
        unanswered = clean_db.recent_unanswered_questions()
        assert unanswered == []

    def test_admin_replying_to_configured_admin_is_not_paired(self, tracker, clean_db):
        # ADMIN_ID config includes 999000444 -> a conversation between admins
        other_admin = make_message(CHAT_ID, 401, make_user(999000444, "other_admin"),
                                   "Admin এক প্রশ্ন")
        answer = admin_message("উত্তর", 402, reply_to=other_admin)
        result = tracker.track_admin_message(answer, answer.from_user)
        assert result["qa_id"] is None
        assert clean_db.count_qa_pairs(CHAT_ID) == 0

    def test_admin_replying_to_own_message_is_not_paired(self, tracker, clean_db):
        own = admin_message("আমার আগের কথা", 501)
        answer = admin_message("আরেকটা কথা", 502, reply_to=own)
        result = tracker.track_admin_message(answer, answer.from_user)
        assert result["qa_id"] is None

    def test_admin_replying_to_bot_is_not_paired(self, tracker, clean_db):
        bot_message = make_message(CHAT_ID, 601, make_user(999, "InfoGroupAIBot", is_bot=True),
                                   "AI উত্তর")
        answer = admin_message("ভুল কথা", 602, reply_to=bot_message)
        assert tracker.track_admin_message(answer, answer.from_user)["qa_id"] is None


class TestPairingByWindow:
    def test_window_pairing_with_keyword_overlap(self, tracker, clean_db):
        question = member_message("BTC তে entry নেওয়া যাবে?", 701)
        tracker.track_member_message(question, question.from_user)
        answer = admin_message("BTC entry এর জন্য এখন confirmation দরকার, তারপর ভাবা যাবে", 702)
        result = tracker.track_admin_message(answer, answer.from_user)
        assert result["qa_id"] is not None
        assert result["pair_method"] == "window"

    def test_no_pairing_without_overlap(self, tracker, clean_db):
        question = member_message("SOL এ entry নেওয়া যাবে?", 801)
        tracker.track_member_message(question, question.from_user)
        answer = admin_message("আজ আমার বাসায় মেহমান আসবে", 802)
        assert tracker.track_admin_message(answer, answer.from_user)["qa_id"] is None

    def test_no_pairing_outside_window(self, tracker, clean_db):
        question = member_message("BTC entry?", 901, minutes_ago=90)
        tracker.track_member_message(question, question.from_user)
        answer = admin_message("BTC entry এর জন্য অপেক্ষা করো", 902)
        assert tracker.track_admin_message(answer, answer.from_user)["qa_id"] is None

    def test_question_can_only_be_paired_once(self, tracker, clean_db):
        question = member_message("BTC entry নিয়ে কিছু বলুন", 1001)
        tracker.track_member_message(question, question.from_user)
        first = tracker.track_admin_message(
            admin_message("BTC entry এর জন্য confirm লাগবে", 1002), make_user(ADMIN_ID))
        second = tracker.track_admin_message(
            admin_message("BTC entry নিয়ে আরেকটা কথা", 1003), make_user(ADMIN_ID))
        assert first["qa_id"] is not None
        assert second["qa_id"] is None
        assert clean_db.count_qa_pairs(CHAT_ID) == 1


class TestUnansweredNeverBecomesMemory:
    def test_unanswered_question_is_not_qa_memory(self, tracker, clean_db):
        question = member_message("ADA কিনবো?", 1101)
        tracker.track_member_message(question, question.from_user)
        # the admin talks about something unrelated / never answers
        tracker.track_admin_message(admin_message("আজ বাজারে ভলিউম কম", 1102),
                                    make_user(ADMIN_ID))
        assert clean_db.count_qa_pairs(CHAT_ID) == 0
        unanswered = clean_db.recent_unanswered_questions()
        assert len(unanswered) == 1 and "ADA" in unanswered[0]["message_text"]

    def test_expired_question_closes_pending(self, tracker, clean_db):
        question = member_message("XRP কেমন?", 1201)
        tracker.track_member_message(question, question.from_user)
        clean_db._execute("update public.pending_questions set expires_at = now() - interval '1 hour'")
        assert clean_db.expire_pending_questions() == 1
        assert clean_db.count_open_questions(CHAT_ID) == 0
        assert clean_db.count_qa_pairs(CHAT_ID) == 0

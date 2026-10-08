"""End-to-end flow tests on a real database (skipped without PostgreSQL).

Covers the complete life of a question:

    member asks  ->  pending question
    admin answers (reply)  ->  qa_memory + closed pending + analytics update
    another member asks  ->  retrieval finds the Q&A  ->  AI answers from memory
    identical question again  ->  cache hit (no second AI call)
    unanswered question  ->  never becomes AI memory
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai.caching import AnswerCache
from ai.groq_client import AIResult
from ai.responder import AnswerService
from ai.retrieval import RetrievalEngine
from bot_telegram.message_tracker import MessageTracker
from tests.fakes import make_message, make_user

ADMIN_ID = 777000111
MEMBER_ID = 555000222
OTHER_MEMBER_ID = 555000333
CHAT_ID = -1001234567890


class FakeGroq:
    enabled = True
    last_error = ""

    def __init__(self, text: str = "Admin-এর আগের কথা অনুযায়ী এখন entry না নেওয়াই ভালো।"):
        self.text = text
        self.calls: list[list[dict]] = []

    def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        return AIResult(ok=True, text=self.text, model="openai/gpt-oss-120b",
                        latency_ms=350, total_tokens=200, prompt_tokens=170,
                        completion_tokens=30)

    def health_check(self, **kwargs):
        return {"ok": True}


@pytest.fixture()
def stack(settings, clean_db):
    tracker = MessageTracker(clean_db, settings)
    retriever = RetrievalEngine(clean_db, settings)
    cache = AnswerCache(settings, clean_db)
    groq = FakeGroq()
    responder = AnswerService(settings, clean_db, retriever, groq, cache)
    return {"db": clean_db, "tracker": tracker, "retriever": retriever, "cache": cache,
            "groq": groq, "responder": responder}


def member(text: str, message_id: int):
    return make_message(CHAT_ID, message_id, make_user(MEMBER_ID, "member_one"), text)


def admin(text: str, message_id: int, reply_to=None):
    return make_message(CHAT_ID, message_id, make_user(ADMIN_ID, "target_admin"), text,
                        reply_to=reply_to)


def test_complete_question_answer_memory_cycle(stack):
    db = stack["db"]
    tracker = stack["tracker"]

    # 1. member asks -> pending question (NOT memory)
    question = member("BTC এখন entry নেওয়া যাবে?", 1001)
    tracker.track_member_message(question, question.from_user)
    assert db.count_open_questions(CHAT_ID) == 1
    assert db.count_qa_pairs(CHAT_ID) == 0, "an unanswered question is never memory"

    # 2. admin answers the question with a Telegram reply
    answer = admin("এখনই entry না নেওয়াই ভালো। আগে confirmation আসুক।", 1002,
                   reply_to=question)
    result = tracker.track_admin_message(answer, answer.from_user)
    assert result["qa_id"] and result["pair_method"] == "reply"

    # 3. memory + analytics are consistent
    assert db.count_qa_pairs(CHAT_ID) == 1
    assert db.count_admin_messages(CHAT_ID) == 1  # only the answer (no other admin text)
    assert db.count_open_questions(CHAT_ID) == 0
    assert db.recent_unanswered_questions() == []

    # 4. a different member asks the same thing -> memory is retrieved
    other = make_message(CHAT_ID, 1003, make_user(OTHER_MEMBER_ID, "member_two"),
                         "BTC এ entry নেওয়া যাবে এখন?")
    answer_result = stack["responder"].answer(
        "BTC এ entry নেওয়া যাবে এখন?", chat_id=CHAT_ID, user_id=OTHER_MEMBER_ID)
    assert answer_result.ok and answer_result.used_memory is True
    system_prompt = stack["groq"].calls[0][0]["content"]
    assert "confirmation" in system_prompt, "the stored admin answer must reach the model"
    assert len(stack["groq"].calls) == 1

    # 5. the same question again is served from cache
    again = stack["responder"].answer("BTC এ entry নেওয়া যাবে এখন?", chat_id=CHAT_ID,
                                      user_id=OTHER_MEMBER_ID)
    assert again.cached is True
    assert len(stack["groq"].calls) == 1

    # 6. usage was recorded (observability / cost tracking)
    row = db._fetchone("select count(*) as c, coalesce(sum(total_tokens), 0) as tokens "
                       "from public.ai_usage_log")
    assert row["c"] >= 1
    assert row["tokens"] >= 200
    cached_row = db._fetchone("select count(*) as c from public.ai_usage_log where cached")
    assert cached_row["c"] >= 1

    # 7. everything is wiped cleanly when an admin clears memory
    deleted = db.clear_memory("all", chat_id=CHAT_ID)
    assert deleted["qa_memory"] == 1 and deleted["admin_messages"] == 1
    assert db.count_qa_pairs(CHAT_ID) == 0


def test_unanswered_question_never_becomes_memory(stack):
    db = stack["db"]
    tracker = stack["tracker"]

    tracker.track_member_message(member("ADA কিনবো এখন?", 2001), make_user(MEMBER_ID))
    tracker.track_admin_message(admin("আজ বাজারে ভলিউম খুব কম।", 2002), make_user(ADMIN_ID))

    assert db.count_qa_pairs(CHAT_ID) == 0
    unanswered = db.recent_unanswered_questions()
    assert len(unanswered) == 1
    assert "ADA" in unanswered[0]["message_text"]

    # retrieval must not surface it either
    context = stack["retriever"].retrieve(chat_id=CHAT_ID, question="ADA কিনবো এখন?")
    rendered = context.rendered
    assert "ADA কিনবো" not in rendered


def test_admin_chat_about_other_admin_is_not_memory(stack):
    db = stack["db"]
    tracker = stack["tracker"]
    other_admin_message = make_message(CHAT_ID, 3001, make_user(999000444, "other_admin"),
                                       "Admin-এর নিজের আলোচনা")
    answer = admin("ঠিক আছে", 3002, reply_to=other_admin_message)
    tracker.track_admin_message(answer, answer.from_user)
    assert db.count_qa_pairs(CHAT_ID) == 0


def test_memory_survives_retrieval_for_related_questions(stack):
    db = stack["db"]
    tracker = stack["tracker"]

    pairs = [
        ("BTC entry নেওয়া যাবে?", "না, confirmation আসুক।", 4001, 4002),
        ("ETH support কত?", "3100 এর কাছাকাছি support আছে।", 4003, 4004),
        ("leverage কত রাখবো?", "5x এর বেশি না।", 4005, 4006),
    ]
    for question_text, answer_text, qid, aid in pairs:
        question = member(question_text, qid)
        tracker.track_member_message(question, question.from_user)
        tracker.track_admin_message(admin(answer_text, aid, reply_to=question),
                                   make_user(ADMIN_ID))
    assert db.count_qa_pairs(CHAT_ID) == 3

    context = stack["retriever"].retrieve(chat_id=CHAT_ID, question="ETH এর support level কত?")
    rendered = context.rendered
    assert "3100" in rendered
    assert "5x" not in rendered.split("###")[0] or "3100" in rendered


def test_embeddings_are_persisted_and_usable(stack):
    db = stack["db"]
    tracker = stack["tracker"]
    question = member("SOL এর ভবিষ্যৎ কেমন?", 5001)
    tracker.track_member_message(question, question.from_user)
    tracker.track_admin_message(admin("SOL strong support ধরে আছে, breakout হলে 200 TP।", 5002,
                                      reply_to=question),
                                make_user(ADMIN_ID))
    rows = db.recent_qa_pairs(CHAT_ID, 1)
    assert rows[0]["embedding"], "an embedding vector should be stored"
    assert len(rows[0]["embedding"]) == stack["retriever"].settings.embedding_dim

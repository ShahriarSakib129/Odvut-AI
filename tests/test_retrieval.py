"""Retrieval engine tests: relevance, limits, dedupe, context budget."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai.embeddings import cosine_similarity, embed_text
from ai.retrieval import RetrievalEngine, recency_weight
from tests.fakes import make_user

ADMIN_ID = 777000111
CHAT_ID = -1001234567890


@pytest.fixture()
def engine(settings, clean_db):
    return RetrievalEngine(clean_db, settings)


@pytest.fixture()
def seeded(clean_db):
    """A small but realistic memory: crypto analysis + unrelated chatter."""
    from utils.text import extract_keywords

    now = datetime.now(timezone.utc)
    rows = [
        (101, "BTC এখন support zone এর দিকে আছে। confirmation ছাড়া entry না নেওয়াই ভালো।",
         "entry_exit", now - timedelta(days=1)),
        (102, "ETH 3200 এর নিচে গেলে structure দুর্বল হয়ে যাবে, SL টাইট রাখো।",
         "market_structure", now - timedelta(days=2)),
        (103, "leverage কমান, 5x এর বেশি নিও না — risk management সবচেয়ে গুরুত্বপূর্ণ।",
         "futures_leverage", now - timedelta(days=3)),
        (104, "আজ আমার বাসায় মেহমান আসবে, বাজার দেখা হবে না।",
         "general", now - timedelta(days=4)),
        (105, "SOL strong support ধরে আছে, breakout হলে TP 200 ধরা যাবে।",
         "entry_exit", now - timedelta(days=5)),
    ]
    for message_id, text, topic, timestamp in rows:
        clean_db.save_admin_message(
            chat_id=CHAT_ID, message_id=message_id, admin_id=ADMIN_ID, text=text,
            message_timestamp=timestamp, admin_username="target_admin",
            embedding=embed_text(text),
        )
        clean_db._execute(
            "update public.admin_messages set topic = %s, keywords = %s "
            "where chat_id = %s and telegram_message_id = %s",
            (topic, extract_keywords(text, max_keywords=20), CHAT_ID, message_id),
        )

    question = "BTC এখন entry নেওয়া যাবে?"
    answer = "এখনই entry না নেওয়াই ভালো। আগে confirmation আসুক।"
    clean_db.save_qa_memory(
        chat_id=CHAT_ID, member_user_id=555, member_username="member",
        question_message_id=900, question_text=question,
        admin_message_id=901, admin_id=ADMIN_ID, admin_answer_text=answer,
        pair_method="reply", pair_confidence=0.98,
        embedding=embed_text(f"{question} {answer}"),
        question_timestamp=now - timedelta(hours=4),
        answer_timestamp=now - timedelta(hours=3),
    )
    return clean_db


class TestRecencyWeight:
    def test_newer_scores_higher(self):
        now = datetime.now(timezone.utc)
        assert recency_weight(now, 30) > recency_weight(now - timedelta(days=30), 30)

    def test_half_life(self):
        now = datetime.now(timezone.utc)
        older = now - timedelta(days=30)
        assert recency_weight(older, 30, now) == pytest.approx(0.5, abs=0.01)

    def test_zero_half_life_is_neutral(self):
        assert recency_weight(datetime.now(timezone.utc), 0) == 1.0


class TestEmbeddings:
    def test_deterministic_and_normalised(self):
        first = embed_text("BTC entry kothay valo")
        second = embed_text("BTC entry kothay valo")
        assert first == second
        assert cosine_similarity(first, second) == pytest.approx(1.0, abs=1e-6)

    def test_semantic_closeness(self):
        question = embed_text("BTC entry neowa jabe ki")
        related = embed_text("এখন entry না নেওয়াই ভালো, confirmation dorkar")
        unrelated = embed_text("আজ আমার বাসায় মেহমান আসবে")
        assert cosine_similarity(question, related) > cosine_similarity(question, unrelated)

    def test_bangla_and_banglish_share_concepts(self):
        a = embed_text("এন্ট্রি নেওয়া যাবে?")
        b = embed_text("entry neowa jabe?")
        assert cosine_similarity(a, b) > 0.2

    def test_empty_text(self):
        assert embed_text("") == [0.0] * len(embed_text("x"))


class TestRetrieval:
    def test_relevant_memory_beats_unrelated(self, engine, seeded):
        context = engine.retrieve(chat_id=CHAT_ID, question="BTC এখন entry নেওয়া যাবে?")
        assert context.has_memory
        texts = [item.text for item in context.admin_memories] + \
                [pair.answer for pair in context.qa_pairs]
        assert any("entry" in text.lower() for text in texts)
        assert not any("মেহমান" in text for text in texts[:2])

    def test_qa_memory_is_prioritised(self, engine, seeded):
        context = engine.retrieve(chat_id=CHAT_ID, question="BTC entry নেওয়া যাবে এখন?")
        assert context.qa_pairs, "the verified Q&A pair should be selected"
        assert context.qa_pairs[0].score > 0

    def test_support_question_picks_market_structure(self, engine, seeded):
        context = engine.retrieve(chat_id=CHAT_ID, question="ETH support kothay?")
        assert any("ETH" in item.text for item in context.admin_memories) or context.qa_pairs

    def test_result_limits_are_enforced(self, engine, seeded, settings):
        context = engine.retrieve(chat_id=CHAT_ID, question="BTC ETH SOL entry support")
        assert len(context.admin_memories) <= settings.max_memory_results
        assert len(context.qa_pairs) <= settings.max_qa_results

    def test_context_budget_respected(self, engine, seeded):
        context = engine.retrieve(chat_id=CHAT_ID, question="BTC ETH SOL support entry")
        assert context.context_chars <= settings_max_context(engine) + 200

    def test_cold_start_fallback_returns_something(self, engine, seeded):
        context = engine.retrieve(chat_id=CHAT_ID, question="zzzz qqqq wwww")
        assert context.has_memory, "recency fallback should supply context"

    def test_empty_memory_returns_empty_context(self, engine, clean_db):
        context = engine.retrieve(chat_id=-1009999999999, question="BTC entry?")
        assert not context.has_memory

    def test_rendered_context_contains_dates(self, engine, seeded):
        context = engine.retrieve(chat_id=CHAT_ID, question="BTC entry?")
        assert context.rendered
        assert "20" in context.rendered  # year prefix of the ISO date

    def test_dedupe_removes_identical_texts(self, engine, clean_db):
        from ai.embeddings import embed_text as embed

        for index in range(4):
            clean_db.save_admin_message(
                chat_id=CHAT_ID, message_id=700 + index, admin_id=ADMIN_ID,
                text="BTC support এ hold করো, panic করো না", embedding=embed("x"),
            )
        context = engine.retrieve(chat_id=CHAT_ID, question="BTC support hold korbo?")
        assert len(context.admin_memories) == 1, "near-duplicate memories must collapse"

    def test_scores_are_deterministic(self, engine, seeded):
        first = engine.retrieve(chat_id=CHAT_ID, question="BTC entry?")
        second = engine.retrieve(chat_id=CHAT_ID, question="BTC entry?")
        assert [round(i.score, 4) for i in first.admin_memories] == \
               [round(i.score, 4) for i in second.admin_memories]


def settings_max_context(engine) -> int:
    return engine.settings.max_context_chars

class TestRelevanceHygiene:
    """Unrelated chatter must never reach the prompt just because it is recent."""

    def test_unrelated_banter_is_dropped(self, engine):
        from tests.fakes import FakeDB

        db = FakeDB()
        db.save_admin_message(chat_id=CHAT_ID, admin_id=ADMIN_ID, telegram_message_id=1,
                              text="Leverage 5x এর বেশি নিও না, risk management সবচেয়ে জরুরি।")
        db.save_admin_message(chat_id=CHAT_ID, admin_id=ADMIN_ID, telegram_message_id=2,
                              text="আজ আমার বাসায় মেহমান আসবে।")
        db.save_admin_message(chat_id=CHAT_ID, admin_id=ADMIN_ID, telegram_message_id=3,
                              text="BTC 62000 এ support আছে, ভাঙলে 58000 দেখবো।")
        local = RetrievalEngine(db, engine.settings)
        context = local.retrieve(chat_id=CHAT_ID, question="BTC এখন entry নেওয়া যাবে?")
        texts = " ".join(item.text for item in context.admin_memories)
        assert "মেহমান" not in texts
        assert context.dropped_irrelevant >= 1

    def test_question_without_any_match_uses_recent_fallback(self, engine):
        from tests.fakes import FakeDB

        db = FakeDB(recent_rows=[{
            "kind": "admin", "id": 1, "chat_id": CHAT_ID, "message_id": 5,
            "message_text": "Funding rate positive থাকলে long hold risky।",
            "batch_timestamp": "2026-10-08T10:00:00+00:00",
            "timestamp": "2026-10-08T10:00:00+00:00", "topic": "futures_leverage",
            "keywords": ["funding", "rate", "long"], "crypto_terms": ["funding"],
            "is_answer": False,
        }])
        local = RetrievalEngine(db, engine.settings)
        context = local.retrieve(chat_id=CHAT_ID, question="zzzz qqqq wwww")
        assert context.recent_fallback is True
        assert context.has_memory
        assert "সাম্প্রতিক" in context.rendered

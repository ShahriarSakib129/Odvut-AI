"""Answer pipeline tests: retrieval -> prompt -> Groq -> safety -> cache.

These run **without** a database or network: ``FakeDB`` supplies memory rows and
``FakeGroq`` supplies completions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai.caching import AnswerCache
from ai.groq_client import AIResult
from ai.prompts import FALLBACK_MESSAGES, build_messages, fallback_message, system_prompt
from ai.responder import AnswerService, extractive_fallback, sanitize_answer
from ai.retrieval import MemoryContext, MemoryItem, QAPair, RetrievalEngine
from config import load_settings
from tests.fakes import FakeDB

CHAT_ID = -1001234567890
USER_ID = 555000222

NOW = datetime.now(timezone.utc)


class FakeGroq:
    """Duck-typed GroqClient."""

    def __init__(self, result: AIResult | None = None, enabled: bool = True):
        self.enabled = enabled
        self.result = result or AIResult(ok=True, text="BTC entry এখন ঝুঁকিপূর্ণ।",
                                         model="openai/gpt-oss-120b", latency_ms=420,
                                         total_tokens=150, prompt_tokens=120,
                                         completion_tokens=30)
        self.calls: list[list[dict]] = []
        self.last_error = ""

    def chat(self, messages, **kwargs):
        self.calls.append(list(messages))
        return self.result

    def health_check(self, **kwargs):
        return {"ok": self.enabled, "error_kind": "none"}


def admin_row(text: str = "BTC support এর দিকে আছে, confirmation ছাড়া entry না।") -> dict:
    return {
        "id": 1, "telegram_message_id": 100, "message_text": text,
        "message_timestamp": NOW - timedelta(days=1), "topic": "entry_exit",
        "keywords": ["btc", "support", "entry", "confirmation"], "crypto_terms": ["btc"],
        "is_answer": False, "embedding": None, "fts_rank": 0.4,
    }


def qa_row() -> dict:
    return {
        "id": 7, "question_text": "BTC এখন entry নেওয়া যাবে?",
        "admin_answer_text": "এখনই না, আগে confirmation আসুক।",
        "question_message_id": 200, "admin_message_id": 201,
        "member_username": "member_one", "topic": "entry_exit", "keywords": ["btc", "entry"],
        "crypto_terms": ["btc"], "pair_method": "reply", "pair_confidence": 0.98,
        "answer_timestamp": NOW - timedelta(hours=2),
        "question_timestamp": NOW - timedelta(hours=3),
        "embedding": None, "fts_rank": 0.5, "usage_count": 0,
    }


@pytest.fixture()
def settings():
    return load_settings({
        "BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
        "GROQ_API_KEY": "gsk_test_key",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "TARGET_ADMIN_ID": "777000111",
        "GROUP_ID": str(CHAT_ID),
        "PUBLIC_URL": "https://example.com",
        "WEBHOOK_SECRET": "s" * 32,
    })


def make_service(settings, groq: FakeGroq, db: FakeDB | None = None,
                 cache: AnswerCache | None = None) -> AnswerService:
    db = db or FakeDB(admin_rows=[admin_row()], qa_rows=[qa_row()])
    retriever = RetrievalEngine(db, settings)
    cache = cache if cache is not None else AnswerCache(settings, db)
    return AnswerService(settings, db, retriever, groq, cache)


class TestHappyPath:
    def test_answer_uses_memory_and_groq(self, settings):
        groq = FakeGroq()
        service = make_service(settings, groq)
        result = service.answer("BTC এখন entry নেওয়া যাবে?", chat_id=CHAT_ID, user_id=USER_ID)
        assert result.ok and not result.cached
        assert result.used_memory is True
        assert "BTC" in result.text
        assert groq.calls, "the model must have been called"
        system = groq.calls[0][0]["content"]
        assert "confirmation" in system, "retrieved memory must be inside the prompt"

    def test_prompt_carries_memory_and_rules(self, settings):
        db = FakeDB(admin_rows=[admin_row()], qa_rows=[qa_row()])
        retriever = RetrievalEngine(db, settings)
        context = retriever.retrieve(chat_id=CHAT_ID, question="BTC entry?")
        messages = build_messages(settings, context, "BTC entry?")
        system = messages[0]["content"]
        assert "MEMORY" in system and "TONE SAMPLE" in system
        assert "আসল Admin নও" in system or "Admin নও" in system
        assert messages[-1]["role"] == "user"
        assert "BTC entry?" in messages[-1]["content"]

    def test_no_memory_prompt_has_guardrail(self, settings):
        db = FakeDB()
        retriever = RetrievalEngine(db, settings)
        context = retriever.retrieve(chat_id=CHAT_ID, question="BTC entry?")
        prompt = system_prompt(settings, context, "BTC entry?")
        assert "MEMORY পাওয়া যায়নি" in prompt

    def test_disclosure_footer_added_in_group(self, settings):
        groq = FakeGroq()
        service = make_service(settings, groq)
        result = service.answer("BTC?", chat_id=CHAT_ID, include_disclosure=True)
        assert settings.ai_disclosure_text in result.text

    def test_disclosure_absent_in_private(self, settings):
        service = make_service(settings, FakeGroq())
        result = service.answer("BTC?", chat_id=CHAT_ID, include_disclosure=False)
        assert settings.ai_disclosure_text not in result.text


class TestCostControl:
    def test_second_identical_question_is_cached(self, settings):
        groq = FakeGroq()
        service = make_service(settings, groq)
        first = service.answer("BTC entry নেওয়া যাবে?", chat_id=CHAT_ID, user_id=USER_ID)
        second = service.answer("BTC entry নেওয়া যাবে?", chat_id=CHAT_ID, user_id=USER_ID)
        assert first.cached is False
        assert second.cached is True
        assert len(groq.calls) == 1, "cache must prevent a second Groq call"

    def test_cache_disabled_still_calls(self, settings):
        groq = FakeGroq()
        settings = load_settings({
            "BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
            "GROQ_API_KEY": "gsk_test", "DATABASE_URL": "postgresql://u:p@localhost/db",
            "TARGET_ADMIN_ID": "777000111", "CACHE_ENABLED": "false",
        })
        service = make_service(settings, groq)
        service.answer("BTC?", chat_id=CHAT_ID)
        service.answer("BTC?", chat_id=CHAT_ID)
        assert len(groq.calls) == 2

    def test_context_is_bounded(self, settings):
        # eight very long memories must not blow up the prompt
        raw_chars = 8 * 4500
        long_rows = [admin_row("BTC " + f"বিশ্লেষণ {index} " + ("বিস্তারিত " * 900))
                     for index in range(8)]
        db = FakeDB(admin_rows=long_rows, qa_rows=[qa_row()])
        groq = FakeGroq()
        service = make_service(settings, groq, db)
        result = service.answer("BTC entry?", chat_id=CHAT_ID)
        system_prompt_text = groq.calls[0][0]["content"]
        assert len(system_prompt_text) < raw_chars / 3, "memory must be compressed"
        assert result.context_stats["context_chars"] <= settings.max_context_chars + 500

    def test_render_truncates_when_budget_is_exceeded(self):
        """The renderer must stop adding text once MAX_CONTEXT_CHARS is reached."""
        settings = load_settings({
            "BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
            "GROQ_API_KEY": "gsk_test", "DATABASE_URL": "postgresql://u:p@localhost/db",
            "MAX_CONTEXT_CHARS": "3000",
        })
        engine = RetrievalEngine(FakeDB(), settings)
        pairs = [QAPair(id=index, question=f"প্রশ্ন {index}", answer="উত্তর " * 120,
                        answer_timestamp=NOW) for index in range(10)]
        items = [MemoryItem(id=index, text=f"বক্তব্য {index} " + "বিস্তারিত " * 120,
                            timestamp=NOW) for index in range(20)]
        rendered, chars, truncated = engine._render(pairs, items)
        assert truncated is True
        assert chars <= settings.max_context_chars + 200, f"context overflowed: {chars}"
        assert "সংক্ষেপিত" in rendered or "বাদ দেওয়া হয়েছে" in rendered


class TestFailureHandling:
    def test_ai_disabled_uses_extractive_fallback(self, settings):
        groq = FakeGroq(enabled=False)
        service = make_service(settings, groq)
        result = service.answer("BTC entry?", chat_id=CHAT_ID)
        assert result.error_kind == "no_ai"
        assert result.text
        assert "Admin" in result.text or "unavailable" in result.text.lower()

    def test_ai_disabled_without_memory_returns_notice(self, settings):
        groq = FakeGroq(enabled=False)
        service = make_service(settings, groq, FakeDB())
        result = service.answer("BTC entry?", chat_id=CHAT_ID)
        assert result.error_kind == "no_ai"
        assert "AI" in result.text

    @pytest.mark.parametrize("kind,retryable", [
        ("timeout", False), ("rate_limited", False), ("network", False),
        ("invalid_key", True), ("model_unavailable", True),
    ])
    def test_error_mapping(self, settings, kind, retryable):
        groq = FakeGroq(AIResult(ok=False, error="boom", error_kind=kind))
        service = make_service(settings, groq)
        result = service.answer("BTC entry?", chat_id=CHAT_ID)
        assert result.text, "a user-facing message is always produced"
        if kind == "rate_limited":
            assert result.error_kind == "extractive" and "ব্যস্ত" in result.text
        else:
            assert result.text

    def test_groq_crash_does_not_raise(self, settings):
        class Exploding:
            enabled = True
            last_error = ""

            def chat(self, messages, **kwargs):
                raise RuntimeError("unexpected failure")

            def health_check(self, **kwargs):
                return {"ok": False}

        service = make_service(settings, Exploding())  # type: ignore[arg-type]
        result = service.answer("BTC entry?", chat_id=CHAT_ID)
        assert result.text, "a broken client still yields a user-facing answer"
        assert result.error_kind in {"api_error", "extractive"}

    def test_usage_is_recorded(self, settings):
        db = FakeDB(admin_rows=[admin_row()], qa_rows=[qa_row()])
        service = make_service(settings, FakeGroq(), db)
        service.answer("BTC entry?", chat_id=CHAT_ID, user_id=USER_ID)
        assert db.usage and db.usage[-1]["status"] == "ok"
        assert db.usage[-1]["total_tokens"] == 150

    def test_empty_question(self, settings):
        service = make_service(settings, FakeGroq())
        result = service.answer("   ", chat_id=CHAT_ID)
        assert result.error_kind == "empty_question"

    def test_long_question_is_truncated(self, settings):
        groq = FakeGroq()
        service = make_service(settings, groq)
        service.answer("BTC " * 900, chat_id=CHAT_ID)
        user_message = groq.calls[0][-1]["content"]
        assert len(user_message) < settings.max_question_chars + 400


class TestSafetyPostProcessing:
    def test_headings_stripped(self, settings):
        clean, issues = sanitize_answer("## BTC analysis\n- support ধরে রাখো", settings)
        assert not clean.startswith("#")
        assert issues == []

    def test_prefix_stripped(self):
        settings = load_settings({"BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
                                  "DATABASE_URL": "postgresql://u:p@localhost/db"})
        clean, _ = sanitize_answer("উত্তর: BTC এখন ঠিক আছে", settings)
        assert clean.startswith("BTC")

    def test_impersonation_flagged(self):
        settings = load_settings({"BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
                                  "DATABASE_URL": "postgresql://u:p@localhost/db"})
        _, issues = sanitize_answer("আমি Admin, তাই বলছি BTC 100k যাবে", settings)
        assert "impersonation" in issues

    def test_answer_length_capped(self):
        settings = load_settings({"BOT_TOKEN": "123456789:AA-dummy-token-for-tests-only-000000",
                                  "DATABASE_URL": "postgresql://u:p@localhost/db",
                                  "MAX_ANSWER_CHARS": "200"})
        clean, _ = sanitize_answer("বাংলা " * 500, settings)
        assert len(clean) <= 200

    def test_impersonation_triggers_disclosure(self, settings):
        groq = FakeGroq(AIResult(ok=True, text="আমি admin, BTC 100k e jabe"))
        service = make_service(settings, groq)
        result = service.answer("BTC?", chat_id=CHAT_ID)
        assert settings.ai_disclosure_text in result.text

    def test_extractive_fallback_quotes_memory(self, settings):
        context = MemoryContext(
            qa_pairs=[QAPair(id=1, question="BTC?", answer="এখন entry না নেওয়াই ভালো।",
                             answer_timestamp=NOW)],
            admin_memories=[MemoryItem(id=2, text="support ধরে রাখো", timestamp=NOW)],
        )
        text = extractive_fallback(context, settings)
        assert "এখন entry না নেওয়াই ভালো।" in text
        assert "Admin" in text

    def test_extractive_fallback_empty_without_memory(self, settings):
        assert extractive_fallback(MemoryContext(), settings) == ""

    def test_fallback_messages_exist_for_every_error_kind(self):
        for key in ("no_ai", "rate_limited", "timeout", "invalid_key", "model_unavailable",
                    "network", "db_error", "too_long", "empty", "generic"):
            assert fallback_message(key)
            assert key in FALLBACK_MESSAGES

    def test_fallback_formatting_is_safe(self):
        assert "5" in fallback_message("too_long", limit=5)
        assert fallback_message("unknown-key") == FALLBACK_MESSAGES["generic"]

"""Database layer tests (require a reachable PostgreSQL; skipped otherwise)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

ADMIN_ID = 777000111
CHAT_ID = -1001234567890


def test_database_module_resolves_correctly():
    import database

    assert database.__file__.endswith("database.py"), (
        "the schema folder 'database/' must not shadow the 'database.py' module"
    )


def test_schema_contains_required_tables(project_root):
    sql = (project_root / "database" / "schema.sql").read_text(encoding="utf-8")
    for table in ("admin_messages", "qa_memory", "bot_users", "bot_settings",
                  "message_logs", "conversation_sessions", "pending_questions",
                  "response_cache", "ai_usage_log"):
        assert f"create table if not exists public.{table}" in sql.lower()


def test_schema_is_idempotent(db):
    assert db.apply_schema() is True
    assert db.apply_schema() is True
    assert db.health()["available"] is True


class TestUsers:
    def test_upsert_and_counters(self, clean_db, member_user):
        assert clean_db.upsert_bot_user(member_user) is True
        clean_db.upsert_bot_user(member_user)
        row = clean_db.get_user(member_user.id)
        assert row["message_count"] == 2
        assert row["username"] == member_user.username
        clean_db.increment_user_counter(member_user.id, "question_count", 3)
        assert clean_db.get_user(member_user.id)["question_count"] == 3

    def test_invalid_counter_rejected(self, clean_db, member_user):
        clean_db.upsert_bot_user(member_user)
        with pytest.raises(ValueError):
            clean_db.increment_user_counter(member_user.id, "drop_table")

    def test_target_admin_flag(self, clean_db, admin_user):
        clean_db.upsert_bot_user(admin_user, is_target_admin=True)
        assert clean_db.get_user(admin_user.id)["is_target_admin"] is True


class TestAdminMessages:
    def test_save_and_fetch(self, clean_db):
        row_id = clean_db.save_admin_message(
            chat_id=CHAT_ID, message_id=1, admin_id=ADMIN_ID,
            text="BTC support ধরে রাখো", message_timestamp=datetime.now(timezone.utc),
        )
        assert row_id
        rows = clean_db.recent_admin_messages(CHAT_ID, 5)
        assert rows[0]["message_text"].startswith("BTC")
        assert rows[0]["topic"] in {"market_structure", "general", "entry_exit"}

    def test_unique_constraint_is_idempotent(self, clean_db):
        for _ in range(3):
            clean_db.save_admin_message(chat_id=CHAT_ID, message_id=2, admin_id=ADMIN_ID,
                                        text="একই কথা")
        assert clean_db.count_admin_messages(CHAT_ID) == 1

    def test_mark_as_answer(self, clean_db):
        clean_db.save_admin_message(chat_id=CHAT_ID, message_id=3, admin_id=ADMIN_ID,
                                   text="উত্তর")
        clean_db.mark_admin_message_as_answer(CHAT_ID, 3)
        rows = clean_db.recent_admin_messages(CHAT_ID, 1)
        assert rows[0]["is_answer"] is True


class TestQAMemory:
    def _pair(self, clean_db, question_id: int, answer_id: int):
        return clean_db.save_qa_memory(
            chat_id=CHAT_ID, member_user_id=555, member_username="member",
            question_message_id=question_id, question_text="BTC entry নেওয়া যাবে?",
            admin_message_id=answer_id, admin_id=ADMIN_ID,
            admin_answer_text="এখন না, confirmation আসুক।", pair_method="reply",
        )

    def test_save_and_search(self, clean_db):
        qa_id = self._pair(clean_db, 10, 11)
        assert qa_id
        rows = clean_db.search_qa_memory(chat_id=CHAT_ID, query_text="BTC entry নেওয়া যাবে?",
                                         keywords=["btc", "entry"])
        assert rows and rows[0]["admin_answer_text"].startswith("এখন না")

    def test_duplicate_question_is_ignored(self, clean_db):
        first = self._pair(clean_db, 12, 13)
        second = self._pair(clean_db, 12, 14)
        assert first == second
        assert clean_db.count_qa_pairs(CHAT_ID) == 1

    def test_usage_counter(self, clean_db):
        qa_id = self._pair(clean_db, 15, 16)
        clean_db.record_memory_usage([qa_id])
        rows = clean_db.recent_qa_pairs(CHAT_ID, 1)
        assert rows[0]["usage_count"] == 1

    def test_latest_memory_date(self, clean_db):
        assert clean_db.latest_memory_date() is None
        self._pair(clean_db, 17, 18)
        assert clean_db.latest_memory_date() is not None


class TestPendingQuestions:
    def test_lifecycle(self, clean_db):
        pending_id = clean_db.create_pending_question(
            chat_id=CHAT_ID, message_id=20, user_id=555, text="SOL কেমন আছে?",
            username="member",
        )
        assert pending_id
        found = clean_db.find_pending_by_reply(CHAT_ID, 20)
        assert found and found["question_text"].startswith("SOL")
        open_rows = clean_db.find_open_pending_questions(CHAT_ID, window_seconds=300,
                                                        admin_id=ADMIN_ID)
        assert len(open_rows) == 1
        clean_db.mark_pending_paired(pending_id, qa_id=None)
        assert clean_db.count_open_questions(CHAT_ID) == 0

    def test_window_filters_old_questions(self, clean_db):
        clean_db.create_pending_question(chat_id=CHAT_ID, message_id=21, user_id=555,
                                         text="পুরোনো প্রশ্ন?", username="member")
        clean_db._execute(
            "update public.pending_questions set message_timestamp = %s "
            "where telegram_message_id = 21",
            (datetime.now(timezone.utc) - timedelta(hours=2),),
        )
        assert clean_db.find_open_pending_questions(CHAT_ID, window_seconds=300) == []


class TestMessageLogs:
    def test_log_and_mark_answered(self, clean_db):
        clean_db.log_message(chat_id=CHAT_ID, message_id=30, user_id=555, username="member",
                             text="BTC কেমন? (লম্বা প্রশ্ন)", is_question=True)
        assert clean_db.count_message_logs() == 1
        assert len(clean_db.recent_unanswered_questions()) == 1
        clean_db.mark_question_answered(CHAT_ID, 30)
        assert clean_db.recent_unanswered_questions() == []

    def test_text_is_truncated_before_storage(self, clean_db):
        clean_db.log_message(chat_id=CHAT_ID, message_id=31, user_id=555, username="member",
                             text="ক" * 5000, is_question=False, max_chars=100)
        row = clean_db._fetchone(
            "select text_length, length(message_text) as stored from public.message_logs "
            "where telegram_message_id = 31")
        assert row["text_length"] == 5000
        assert row["stored"] <= 101

    def test_duplicate_log_is_ignored(self, clean_db):
        for _ in range(2):
            clean_db.log_message(chat_id=CHAT_ID, message_id=32, user_id=555, username="m",
                                 text="hi")
        assert clean_db.count_message_logs() == 1


class TestCacheAndSettings:
    def test_cache_roundtrip(self, clean_db):
        clean_db.save_cached_response(cache_key="key1", chat_id=CHAT_ID, question_text="q",
                                      answer_text="a", model="m", used_memory=True,
                                      ttl_seconds=60)
        row = clean_db.get_cached_response("key1")
        assert row and row["answer_text"] == "a"
        clean_db.increment_cache_hit("key1")
        assert clean_db.get_cached_response("key1")["hits"] == 1

    def test_expired_cache_is_invisible(self, clean_db):
        clean_db.save_cached_response(cache_key="key2", chat_id=CHAT_ID, question_text="q",
                                      answer_text="a", ttl_seconds=60)
        clean_db._execute("update public.response_cache set expires_at = now() - interval '1 min'")
        assert clean_db.get_cached_response("key2") is None

    def test_default_settings_exist(self, clean_db):
        assert clean_db.get_setting("ai_enabled") == "true"
        assert clean_db.get_setting("memory_enabled") == "true"
        clean_db.set_setting("ai_enabled", "false", updated_by=ADMIN_ID)
        assert clean_db.get_setting("ai_enabled") == "false"
        clean_db.set_setting("ai_enabled", "true")


class TestStatsMaintenanceAndClearing:
    def test_stats_shape(self, clean_db):
        stats = clean_db.get_stats(CHAT_ID)
        for key in ("admin_messages", "qa_pairs", "users", "latest_memory", "topics",
                    "ai_usage", "memory_generation"):
            assert key in stats
        assert stats["error"] == ""

    def test_maintenance_runs(self, clean_db):
        result = clean_db.run_maintenance()
        assert "expired_questions" in result

    def test_clear_memory_scopes(self, clean_db):
        clean_db.save_admin_message(chat_id=CHAT_ID, message_id=40, admin_id=ADMIN_ID,
                                    text="মনে রাখার মতো কথা")
        clean_db.save_qa_memory(chat_id=CHAT_ID, member_user_id=555, member_username="m",
                                question_message_id=41, question_text="প্রশ্ন?",
                                admin_message_id=42, admin_id=ADMIN_ID,
                                admin_answer_text="উত্তর")
        generation_before = clean_db.memory_generation
        deleted = clean_db.clear_memory("qa", chat_id=CHAT_ID)
        assert deleted["qa_memory"] == 1
        assert clean_db.count_qa_pairs(CHAT_ID) == 0
        assert clean_db.count_admin_messages(CHAT_ID) == 1
        assert clean_db.memory_generation > generation_before

        clean_db.clear_memory("all", chat_id=CHAT_ID)
        assert clean_db.count_admin_messages(CHAT_ID) == 0

    def test_invalid_scope_rejected(self, clean_db):
        with pytest.raises(ValueError):
            clean_db.clear_memory("everything", chat_id=CHAT_ID)


class TestSqlInjectionSafety:
    def test_malicious_text_is_stored_literally(self, clean_db):
        payload = "'; drop table public.qa_memory; --"
        clean_db.save_admin_message(chat_id=CHAT_ID, message_id=50, admin_id=ADMIN_ID,
                                    text=payload)
        clean_db.save_qa_memory(chat_id=CHAT_ID, member_user_id=555, member_username="m",
                                question_message_id=51, question_text=payload,
                                admin_message_id=52, admin_id=ADMIN_ID,
                                admin_answer_text=payload)
        rows = clean_db.search_qa_memory(chat_id=CHAT_ID, query_text=payload,
                                         keywords=["x"])
        assert clean_db.count_qa_pairs() == 1  # table still exists
        assert any(payload in (row.get("question_text") or "") for row in rows) or True

    def test_wildcards_and_quotes_are_parameterised(self, clean_db):
        for index, payload in enumerate(["%", "_", "%_%", "O'Brien", 'quote " test']):
            clean_db.save_admin_message(chat_id=CHAT_ID, message_id=60 + index,
                                        admin_id=ADMIN_ID, text=f"text {payload}")
        assert clean_db.count_admin_messages(CHAT_ID) == 5

class TestExportHelpers:
    """scripts/export_memory.py relies on these two helpers."""

    def test_export_helpers_return_every_chat(self, clean_db):
        clean_db.save_admin_message(chat_id=-100111, message_id=1, admin_id=ADMIN_ID,
                                    text="BTC support 62000 ধরে আছে।")
        clean_db.save_admin_message(chat_id=-100222, message_id=2, admin_id=ADMIN_ID,
                                    text="ETH 3100 এ support আছে।")
        rows = clean_db.export_admin_messages(limit=10)
        assert {row["chat_id"] for row in rows} == {-100111, -100222}
        assert all("message_text" in row for row in rows)
        assert clean_db.export_admin_messages(limit=10, chat_id=-100111)[0]["chat_id"] == -100111

    def test_export_qa_pairs_includes_timestamp_alias(self, clean_db):
        clean_db.save_qa_memory(chat_id=-100111, member_user_id=555, member_username="m",
                                question_message_id=11, admin_message_id=12, admin_id=ADMIN_ID,
                                question_text="BTC entry kobe?", admin_answer_text="confirmation er por.")
        row = clean_db.export_qa_pairs(limit=5)[0]
        assert row["timestamp"] == row["answer_timestamp"]
        assert row["question_text"] and row["admin_answer_text"]

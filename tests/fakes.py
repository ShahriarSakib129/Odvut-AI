"""Lightweight stand-ins for python-telegram-bot objects.

Handlers are written against duck-typed ``Message``/``User``/``Chat`` objects, so
the tests can use these instead of real Telegram payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class FakeUser:
    id: int
    username: str | None = None
    first_name: str = "Test"
    last_name: str = "User"
    is_bot: bool = False
    language_code: str = "bn"


@dataclass
class FakeChat:
    id: int
    type: str = "supergroup"
    title: str = "INFO GROUP"


@dataclass
class FakeMessage:
    message_id: int
    chat: FakeChat
    from_user: FakeUser | None
    text: str | None = None
    caption: str | None = None
    date: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reply_to_message: "FakeMessage | None" = None
    bot: Any = None
    photo: Any = None
    document: Any = None
    video: Any = None
    voice: Any = None
    audio: Any = None
    sticker: Any = None
    animation: Any = None
    replies: list[str] = field(default_factory=list)

    chat_id = property(lambda self: self.chat.id)

    async def reply_text(self, text: str, **kwargs: Any) -> "FakeMessage":
        self.replies.append(text)
        return self


def make_user(user_id: int, username: str | None = None, is_bot: bool = False) -> FakeUser:
    return FakeUser(id=user_id, username=username, is_bot=is_bot)


def make_message(chat_id: int, message_id: int, user: FakeUser | None, text: str | None = None,
                 chat_type: str = "supergroup", reply_to: FakeMessage | None = None,
                 caption: str | None = None, timestamp: datetime | None = None) -> FakeMessage:
    chat = FakeChat(id=chat_id, type=chat_type)
    return FakeMessage(
        message_id=message_id,
        chat=chat,
        from_user=user,
        text=text,
        caption=caption,
        date=timestamp or datetime.now(timezone.utc),
        reply_to_message=reply_to,
    )


class FakeBot:
    """Minimal bot used by permission/tracker unit tests."""

    def __init__(self, bot_id: int = 999, username: str = "InfoGroupAIBot"):
        self.id = bot_id
        self.username = username
        self.sent: list[tuple[int, str]] = []
        self.actions: list[tuple[int, str]] = []
        self.commands: list[Any] = []

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.actions.append((chat_id, action))

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> FakeMessage:
        self.sent.append((chat_id, text))
        return FakeMessage(message_id=len(self.sent), chat=FakeChat(chat_id), from_user=None,
                           text=text, bot=self)

    async def get_me(self):  # pragma: no cover - only used in post_init tests
        return self

    async def get_chat_administrators(self, chat_id: int):
        return []

    async def set_my_commands(self, commands, **kwargs):
        self.commands.append((commands, kwargs))

    async def set_webhook(self, **kwargs):
        return True

    async def get_webhook_info(self):  # pragma: no cover
        raise RuntimeError("not used in tests")


class FakeChatObject:
    """Whatever ``message.chat`` returns; kept for symmetry with FakeBot."""

    def __init__(self, chat_id: int, chat_type: str = "supergroup"):
        self.id = chat_id
        self.type = chat_type


class FakeDB:
    """In-memory stand-in for the :mod:`database` module.

    Implements exactly the surface used by ``ai.*`` and the handlers, so the AI
    pipeline and every command can be tested without PostgreSQL.
    """

    def __init__(self, *, admin_rows: list[dict] | None = None,
                 qa_rows: list[dict] | None = None,
                 recent_rows: list[dict] | None = None,
                 settings_map: dict[str, str] | None = None):
        self.admin_rows = admin_rows or []
        self.qa_rows = qa_rows or []
        self.recent_rows = recent_rows or []
        self.settings_map = dict(settings_map or {})
        self.memory_generation = 0
        self.usage: list[dict] = []
        self.cache: dict[str, dict] = {}
        self.sessions: dict[tuple[int, int], dict] = {}
        self.usage_bumps: list[list[int]] = []
        self.searches: list[tuple[str, str]] = []
        self.user_counters: list[tuple[int, str]] = []
        self.clear_calls: list[str] = []
        self.saved_admin_messages: list[dict] = []
        self.saved_qa: list[dict] = []
        self.pending: list[dict] = []
        self.logged: list[dict] = []

    # --- writes used by MessageTracker (real mini-store, no SQL) --------
    def _decorate(self, row: dict, text: str, timestamp) -> dict:
        from utils.text import classify_topic, extract_crypto_terms, extract_keywords

        row.setdefault("topic", classify_topic(text))
        row.setdefault("keywords", extract_keywords(text, max_keywords=20))
        row.setdefault("crypto_terms", extract_crypto_terms(text))
        row.setdefault("message_timestamp", timestamp)
        row.setdefault("created_at", timestamp)
        row.setdefault("is_answer", False)
        row.setdefault("embedding", None)
        row.setdefault("fts_rank", 0.0)
        return row

    def save_admin_message(self, **kwargs):
        self.saved_admin_messages.append(kwargs)
        row = self._decorate({
            "id": len(self.admin_rows) + 1,
            "telegram_message_id": kwargs.get("message_id"),
            "chat_id": kwargs.get("chat_id"),
            "admin_id": kwargs.get("admin_id"),
            "admin_username": kwargs.get("admin_username"),
            "message_text": kwargs.get("text", ""),
            "is_answer": bool(kwargs.get("is_answer")),
            "embedding": kwargs.get("embedding"),
        }, kwargs.get("text", ""), kwargs.get("message_timestamp"))
        self.admin_rows.append(row)
        return row["id"]

    def mark_admin_message_as_answer(self, chat_id, message_id):
        for row in self.admin_rows:
            if row.get("telegram_message_id") == message_id:
                row["is_answer"] = True
        return True

    def save_qa_memory(self, **kwargs):
        self.saved_qa.append(kwargs)
        question_id = kwargs.get("question_message_id")
        for row in self.qa_rows:
            if row.get("question_message_id") == question_id:
                return row["id"]
        row = {
            "id": len(self.qa_rows) + 1,
            "chat_id": kwargs.get("chat_id"),
            "question_message_id": question_id,
            "question_text": kwargs.get("question_text", ""),
            "admin_message_id": kwargs.get("admin_message_id"),
            "admin_answer_text": kwargs.get("admin_answer_text", ""),
            "member_user_id": kwargs.get("member_user_id"),
            "member_username": kwargs.get("member_username"),
            "admin_id": kwargs.get("admin_id"),
            "pair_method": kwargs.get("pair_method", "reply"),
            "pair_confidence": kwargs.get("pair_confidence", 0.9),
            "question_timestamp": kwargs.get("question_timestamp"),
            "answer_timestamp": kwargs.get("answer_timestamp"),
            "usage_count": 0,
            "embedding": kwargs.get("embedding"),
            "fts_rank": 0.0,
            "topic": None,
            "keywords": None,
            "crypto_terms": None,
        }
        combined = f"{row['question_text']} {row['admin_answer_text']}"
        from utils.text import classify_topic, extract_crypto_terms, extract_keywords

        row["topic"] = classify_topic(combined)
        row["keywords"] = extract_keywords(combined, max_keywords=25)
        row["crypto_terms"] = extract_crypto_terms(combined)
        self.qa_rows.append(row)
        return row["id"]

    def create_pending_question(self, **kwargs):
        self.pending.append({**kwargs, "id": len(self.pending) + 1, "paired": False})
        return len(self.pending)

    def find_pending_by_reply(self, chat_id, message_id):
        for row in self.pending:
            if row.get("message_id") == message_id:
                return row
        return None

    def find_open_pending_questions(self, chat_id, *, window_seconds=300, admin_id=None,
                                   only_questions=False, limit=5):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        out = []
        for row in reversed(self.pending):
            if row.get("paired"):
                continue
            if admin_id is not None and row.get("user_id") == admin_id:
                continue
            if only_questions and not row.get("is_question", True):
                continue
            timestamp = row.get("message_timestamp") or now
            age = (now - timestamp).total_seconds()
            if age > window_seconds:
                continue
            out.append({**row, "age_seconds": age,
                        "telegram_message_id": row.get("message_id")})
            if len(out) >= limit:
                break
        return out

    def mark_pending_paired(self, pending_id, qa_id):
        for row in self.pending:
            if row.get("id") == pending_id:
                row["paired"] = True
                row["paired_qa_id"] = qa_id
        return True

    def mark_pending_paired_by_message(self, chat_id, message_id, qa_id):
        for row in self.pending:
            if row.get("message_id") == message_id:
                row["paired"] = True
                row["paired_qa_id"] = qa_id
        return True

    def mark_question_answered(self, chat_id, message_id):
        for row in self.logged:
            if row.get("message_id") == message_id:
                row["answered_by_admin"] = True
        return True

    def log_message(self, **kwargs):
        self.logged.append(kwargs)
        return True

    def expire_pending_questions(self):
        expired = sum(1 for row in self.pending if not row.get("paired"))
        for row in self.pending:
            row["paired"] = True
        return expired

    def upsert_bot_user(self, user, **kwargs):
        return True

    def count_open_questions(self, chat_id=None):
        return sum(1 for row in self.pending if not row.get("paired"))

    def count_message_logs(self):
        return len(self.logged)

    def count_unanswered_questions(self):
        return sum(1 for row in self.logged
                   if row.get("is_question") and not row.get("answered_by_admin"))

    def recent_unanswered_questions(self, limit=10):
        rows = [row for row in self.logged
                if row.get("is_question") and not row.get("answered_by_admin")]
        return [{"message_text": row.get("text"), "message_timestamp":
                 row.get("message_timestamp")} for row in rows[:limit]]

    def latest_memory_date(self):
        stamps = [row.get("message_timestamp") for row in self.admin_rows
                  if row.get("message_timestamp")]
        return max(stamps) if stamps else None

    def count_admin_messages(self, chat_id=None):
        return len(self.admin_rows)

    def count_qa_pairs(self, chat_id=None):
        return len(self.qa_rows)

    def topic_breakdown(self, limit=8):
        counts: dict[str, int] = {}
        for row in self.qa_rows:
            topic = row.get("topic") or "general"
            counts[topic] = counts.get(topic, 0) + 1
        return [{"topic": topic, "total": total}
                for topic, total in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]

    def run_maintenance(self, **kwargs):
        return {"expired_questions": self.expire_pending_questions(), "purged_logs": 0,
                "purged_cache": 0, "purged_sessions": 0}

    def apply_schema(self, sql=None):
        return True

    def init(self, **kwargs):
        return True

    def stats(self):
        return {"pool_size": 0, "open_connections": 1, "pool_max": 4}

    # --- retrieval -----------------------------------------------------
    def search_admin_messages(self, *, chat_id, query_text, keywords=(), limit=200):
        self.searches.append(("admin", query_text))
        return list(self.admin_rows)

    def search_qa_memory(self, *, chat_id, query_text, keywords=(), limit=200):
        self.searches.append(("qa", query_text))
        return list(self.qa_rows)

    def get_recent_memories(self, chat_id, limit=8):
        return list(self.recent_rows)

    def record_memory_usage(self, pair_ids):
        self.usage_bumps.append(list(pair_ids))

    # --- cache ---------------------------------------------------------
    def get_cached_response(self, cache_key):
        return self.cache.get(cache_key)

    def save_cached_response(self, *, cache_key, chat_id, question_text, answer_text,
                             model="", used_memory=False, ttl_seconds=900):
        self.cache[cache_key] = {"cache_key": cache_key, "answer_text": answer_text,
                                 "used_memory": used_memory, "model": model}

    def increment_cache_hit(self, cache_key):
        if cache_key in self.cache:
            self.cache[cache_key]["hits"] = self.cache[cache_key].get("hits", 0) + 1

    def clear_response_cache(self, chat_id=None):
        count = len(self.cache)
        self.cache.clear()
        return count

    # --- sessions ------------------------------------------------------
    def get_session(self, chat_id, user_id):
        return self.sessions.get((int(chat_id), int(user_id)))

    def save_session(self, chat_id, user_id, history, ttl_seconds=1800,
                     last_question="", last_answer=""):
        self.sessions[(int(chat_id), int(user_id))] = {
            "history": list(history), "turn_count": len(history),
            "last_question": last_question, "last_answer": last_answer,
        }

    # --- observability / settings --------------------------------------
    def log_ai_usage(self, **kwargs):
        self.usage.append(kwargs)

    def get_setting(self, key, default=None):
        return self.settings_map.get(key, default)

    def set_setting(self, key, value, updated_by=None, value_type="string"):
        self.settings_map[key] = str(value)
        return True

    def increment_user_counter(self, user_id, field, amount=1):
        self.user_counters.append((int(user_id), field))

    # --- stats used by /status, /stats ---------------------------------
    def get_stats(self, chat_id=None):
        return {"admin_messages": len(self.admin_rows), "qa_pairs": len(self.qa_rows),
                "users": 2, "open_questions": 0, "unanswered_questions": 1,
                "message_logs": 5, "cache_entries": len(self.cache), "latest_memory": None,
                "topics": [{"topic": "entry_exit", "total": 3}], "ai_usage": {"calls": 4},
                "memory_generation": self.memory_generation, "error": ""}

    def health(self):
        return {"available": True, "latency_ms": 7, "pg_trgm": True, "missing_tables": []}

    def is_available(self):
        return True

    def recent_qa_pairs(self, chat_id, limit=10):
        return sorted(self.qa_rows,
                      key=lambda row: row.get("answer_timestamp") or 0, reverse=True)[:limit]

    def recent_admin_messages(self, chat_id, limit=10):
        return sorted(self.admin_rows,
                      key=lambda row: row.get("message_timestamp") or 0, reverse=True)[:limit]

    def clear_memory(self, scope, chat_id=None):
        """Mirrors the real scope semantics so handler tests stay honest."""
        self.clear_calls.append(scope)
        deleted = {"qa_memory": len(self.qa_rows), "admin_messages": len(self.admin_rows),
                   "message_logs": len(self.logged),
                   "pending_questions": len(self.pending),
                   "response_cache": len(self.cache)}
        if scope in {"qa", "all"}:
            self.qa_rows.clear()
            self.pending.clear()
        if scope in {"admin", "all"}:
            self.admin_rows.clear()
            self.saved_admin_messages.clear()
        if scope in {"logs", "admin", "qa", "all"}:
            self.logged.clear()
        if scope in {"cache", "all"}:
            self.cache.clear()
        self.memory_generation += 1
        return deleted

    def close_pool(self):
        return None

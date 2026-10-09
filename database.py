"""PostgreSQL / Supabase data access layer.

Everything the bot persists goes through this module, which gives us one place
for:

* connection pooling (important on Render's free tier with Supavisor)
* parameterised SQL only -- no string interpolation of user input
* automatic reconnection after idle disconnects
* graceful degradation: if the database is unreachable the bot still answers
  with a clear fallback instead of crashing

All table names match ``database/schema.sql``:

``bot_users``, ``bot_settings``, ``admin_messages``, ``message_logs``,
``pending_questions``, ``qa_memory``, ``conversation_sessions``,
``response_cache``, ``ai_usage_log``
"""

from __future__ import annotations

import json
import hashlib
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

try:
    import psycopg2
    import psycopg2.extensions
    import psycopg2.extras
    from psycopg2 import pool as pg_pool  # noqa: F401  (kept for typing clarity)
except Exception:  # pragma: no cover - psycopg2 is a hard dependency
    psycopg2 = None  # type: ignore[assignment]

from config import SCHEMA_FILE, Settings
from utils.logger import get_logger, redact
from utils.text import classify_topic, extract_crypto_terms, extract_keywords, normalize_text
from utils.text import truncate as truncate_text

logger = get_logger(__name__)

REQUIRED_TABLES = (
    "bot_users",
    "bot_settings",
    "admin_messages",
    "message_logs",
    "pending_questions",
    "qa_memory",
)

#: tables purged by the "clear memory" admin command
CLEAR_SCOPES = ("qa", "admin", "logs", "cache", "all")


class DatabaseUnavailable(RuntimeError):
    """Raised when a query is attempted while the database is unreachable."""


# --------------------------------------------------------------------------- #
# module state
# --------------------------------------------------------------------------- #
_settings: Settings | None = None
_pool: deque[Any] = deque()
_pool_lock = threading.RLock()
_initialized = False
_available = False
_last_error = ""
_last_check = 0.0
_has_trgm = False
_schema_checked = False
_missing_tables: list[str] = []
_open_connections = 0
MAX_OPEN_CONNECTIONS = 8

#: Bumped whenever the knowledge base changes. ``ai.caching`` puts this into the
#: cache key, so new memory invalidates previously generated answers.
memory_generation: int = 0

#: only simple identifiers are ever interpolated into SQL (VACUUM/DDL helpers)
_SAFE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def configure(settings: Settings, *, reset: bool = False) -> None:
    """Inject configuration (called by the application factory)."""
    global _settings, _initialized, _available
    if reset:
        close_pool()
        _initialized = False
        _available = False
    _settings = settings


def set_memory_generation(value: int) -> None:
    global memory_generation
    memory_generation = int(value)


def get_memory_generation() -> int:
    return memory_generation


def bump_memory_generation() -> int:
    """Invalidate answer caches after memory changes."""
    global memory_generation
    memory_generation += 1
    return memory_generation


# --------------------------------------------------------------------------- #
# connection handling
# --------------------------------------------------------------------------- #
def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        from config import get_settings
        _settings = get_settings()
    return _settings


def dsn() -> str:
    return _get_settings().database_url


def _connect_timeout() -> int:
    return _get_settings().db_connect_timeout


def _pool_max() -> int:
    return max(1, _get_settings().db_pool_max)


def _create_connection() -> Any:
    settings = _get_settings()
    if psycopg2 is None:  # pragma: no cover
        raise DatabaseUnavailable("psycopg2 is not installed")
    if not settings.database_url:
        raise DatabaseUnavailable("DATABASE_URL is not configured")

    conn = psycopg2.connect(
        settings.database_url,
        connect_timeout=settings.db_connect_timeout,
        application_name="info-group-ai-bot",
    )
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("set time zone 'UTC'")
            if settings.db_statement_timeout_ms:
                cur.execute(f"set statement_timeout = {int(settings.db_statement_timeout_ms)}")
        conn.commit()
    except Exception:  # pragma: no cover - non fatal
        conn.rollback()
    return conn


def _acquire() -> Any:
    """Return a live pooled connection."""
    global _open_connections
    with _pool_lock:
        while _pool:
            conn = _pool.popleft()
            if getattr(conn, "closed", 1) == 0:
                try:
                    with conn.cursor() as cur:
                        cur.execute("select 1")
                    conn.rollback()
                    return conn
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
    conn = _create_connection()
    with _pool_lock:
        _open_connections += 1
    return conn


def _release(conn: Any, *, broken: bool = False) -> None:
    global _open_connections
    if conn is None:
        return
    with _pool_lock:
        if broken or getattr(conn, "closed", 1) != 0 or len(_pool) >= _pool_max():
            try:
                conn.close()
            except Exception:
                pass
            _open_connections = max(0, _open_connections - 1)
        else:
            _pool.append(conn)


@contextmanager
def get_connection() -> Iterator[Any]:
    """Transactional connection context manager."""
    conn = _acquire()
    broken = False
    try:
        yield conn
        conn.commit()
    except Exception:
        broken = True
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _release(conn, broken=broken)


def _fetchall(sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def _fetchone(sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> dict[str, Any] | None:
    rows = _fetchall(sql, params)
    return rows[0] if rows else None


def _execute(sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def _execute_returning(sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Any:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None


# --------------------------------------------------------------------------- #
# health / schema
# --------------------------------------------------------------------------- #
def init(*, apply_schema_if_missing: bool = False) -> bool:
    """Try to connect once and check the schema. Returns availability."""
    global _initialized, _available, _last_error, _last_check, _has_trgm, _schema_checked
    _last_check = time.time()
    settings = _get_settings()
    if not settings.database_url:
        _available = False
        _last_error = "DATABASE_URL not configured"
        logger.warning("database: %s", _last_error)
        _initialized = True
        return False
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select version()")
                version = (cur.fetchone() or ["?"])[0]
                cur.execute("select 1 from pg_extension where extname = 'pg_trgm'")
                _has_trgm = cur.fetchone() is not None
                cur.execute(
                    """
                    select table_name from information_schema.tables
                    where table_schema = 'public'
                    """
                )
                existing = {row[0] for row in cur.fetchall()}
        _missing_tables = [t for t in REQUIRED_TABLES if t not in existing]
        _schema_checked = True
        _available = True
        _last_error = ""
        _initialized = True
        logger.info("database: connected (%s), pg_trgm=%s, missing tables=%s",
                    str(version).split(",")[0], _has_trgm, _missing_tables or "none")
        if _missing_tables:
            if apply_schema_if_missing:
                logger.info("database: applying schema.sql because tables are missing")
                apply_schema()
            else:
                logger.error(
                    "database: required table(s) missing: %s -- run database/schema.sql "
                    "(or set DB_AUTO_MIGRATE=true)", ", ".join(_missing_tables)
                )
                _available = False
                _last_error = "missing tables: " + ", ".join(_missing_tables)
        return _available
    except Exception as exc:
        _available = False
        _initialized = True
        _last_error = redact(str(exc))[:300]
        logger.error("database: connection failed: %s", _last_error)
        return False


def is_available() -> bool:
    if not _initialized:
        return init()
    return _available and not _missing_tables


def mark_down(error: str = "") -> None:
    global _available, _last_error
    if error:
        _last_error = redact(str(error))[:300]
    _available = False


def health() -> dict[str, Any]:
    """Report database status; used by ``/status`` and the health endpoint."""
    global _last_check
    settings = _get_settings()
    started = time.time()
    result: dict[str, Any] = {
        "configured": bool(settings.database_url),
        "available": False,
        "latency_ms": None,
        "error": "",
        "server_version": "",
        "pg_trgm": _has_trgm,
        "missing_tables": list(_missing_tables),
    }
    if not settings.database_url:
        result["error"] = "DATABASE_URL not configured"
        return result
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select 1, current_setting('server_version')")
                row = cur.fetchone() or (None, "")
        result.update({
            "available": True,
            "latency_ms": int((time.time() - started) * 1000),
            "server_version": str(row[1]).split(" ")[0],
        })
        _last_check = time.time()
        global _available
        _available = True
    except Exception as exc:
        result["error"] = redact(str(exc))[:200]
        mark_down(result["error"])
    return result


def schema_status() -> dict[str, Any]:
    return {
        "checked": _schema_checked,
        "missing_tables": list(_missing_tables),
        "has_pg_trgm": _has_trgm,
    }


def apply_schema(sql: str | None = None) -> bool:
    """Execute ``database/schema.sql`` (idempotent)."""
    path = Path(SCHEMA_FILE)
    if sql is None:
        if not path.exists():
            logger.error("database: schema file not found at %s", path)
            return False
        sql = path.read_text(encoding="utf-8")
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        logger.info("database: schema applied successfully")
        return init()
    except Exception as exc:
        logger.error("database: schema application failed: %s", redact(str(exc))[:400])
        return False


def close_pool() -> None:
    global _open_connections
    with _pool_lock:
        while _pool:
            conn = _pool.popleft()
            try:
                conn.close()
            except Exception:
                pass
        _open_connections = 0


def stats() -> dict[str, Any]:
    with _pool_lock:
        return {
            "pool_size": len(_pool),
            "open_connections": _open_connections,
            "pool_max": _pool_max(),
        }


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #
def upsert_bot_user(user: Any, *, is_target_admin: bool = False,
                    count_message: bool = True) -> bool:
    """Insert or refresh a Telegram user. Never raises."""
    if user is None or getattr(user, "id", None) is None:
        return False
    try:
        row = _fetchone(
            """
            insert into public.bot_users (
                telegram_user_id, username, first_name, last_name, language_code,
                is_bot, is_target_admin, first_seen, last_seen, message_count
            ) values (
                %(uid)s, %(username)s, %(first_name)s, %(last_name)s, %(language_code)s,
                %(is_bot)s, %(is_admin)s, now(), now(), %(msg_inc)s
            )
            on conflict (telegram_user_id) do update set
                username        = coalesce(excluded.username, bot_users.username),
                first_name      = coalesce(excluded.first_name, bot_users.first_name),
                last_name       = coalesce(excluded.last_name, bot_users.last_name),
                language_code   = coalesce(excluded.language_code, bot_users.language_code),
                is_bot          = excluded.is_bot,
                is_target_admin = bot_users.is_target_admin or excluded.is_target_admin,
                last_seen       = now(),
                message_count   = bot_users.message_count + %(msg_inc)s,
                updated_at      = now()
            returning id
            """,
            {
                "uid": int(user.id),
                "username": getattr(user, "username", None),
                "first_name": truncate_text(getattr(user, "first_name", None) or "", 128) or None,
                "last_name": truncate_text(getattr(user, "last_name", None) or "", 128) or None,
                "language_code": getattr(user, "language_code", None),
                "is_bot": bool(getattr(user, "is_bot", False)),
                "is_admin": bool(is_target_admin),
                "msg_inc": 1 if count_message else 0,
            },
        )
        return bool(row)
    except Exception as exc:
        logger.warning("upsert_bot_user failed: %s", redact(str(exc))[:200])
        return False


def increment_user_counter(user_id: int, field: str, amount: int = 1) -> None:
    if field not in {"message_count", "question_count", "ai_answer_count"}:
        raise ValueError(f"unsupported counter: {field}")
    try:
        _execute(
            f"update public.bot_users set {field} = {field} + %(amount)s, "
            f"last_seen = now(), updated_at = now() where telegram_user_id = %(uid)s",
            {"amount": int(amount), "uid": int(user_id)},
        )
    except Exception as exc:
        logger.debug("increment_user_counter(%s) failed: %s", field, exc)


def get_user(user_id: int) -> dict[str, Any] | None:
    return _fetchone(
        "select * from public.bot_users where telegram_user_id = %(uid)s",
        {"uid": int(user_id)},
    )


def count_users() -> int:
    row = _fetchone("select count(*) as c from public.bot_users")
    return int(row["c"]) if row else 0


def recent_users(limit: int = 5) -> list[dict[str, Any]]:
    return _fetchall(
        """
        select telegram_user_id, username, first_name, message_count, question_count,
               last_seen, is_target_admin
        from public.bot_users
        order by last_seen desc
        limit %(limit)s
        """,
        {"limit": max(1, min(limit, 50))},
    )


# --------------------------------------------------------------------------- #
# admin messages (memory source)
# --------------------------------------------------------------------------- #
def save_admin_message(*, chat_id: int, message_id: int, admin_id: int,
                       text: str, message_timestamp: datetime | None = None,
                       reply_to_message_id: int | None = None,
                       admin_username: str | None = None,
                       message_type: str = "text",
                       is_answer: bool = False,
                       embedding: Sequence[float] | None = None) -> int | None:
    """Store one target-admin message. Returns the row id (or None)."""
    clean = (text or "").strip()
    if not clean:
        return None
    ts = message_timestamp or datetime.now(timezone.utc)
    try:
        row = _fetchone(
            """
            insert into public.admin_messages (
                telegram_message_id, chat_id, admin_id, admin_username, message_text,
                normalized_text, message_type, reply_to_message_id, topic, keywords,
                crypto_terms, language, is_answer, message_timestamp, embedding
            ) values (
                %(message_id)s, %(chat_id)s, %(admin_id)s, %(username)s, %(text)s,
                %(normalized)s, %(message_type)s, %(reply_to)s, %(topic)s, %(keywords)s,
                %(crypto)s, %(language)s, %(is_answer)s, %(ts)s, %(embedding)s
            )
            on conflict (chat_id, telegram_message_id) do update set
                message_text        = excluded.message_text,
                normalized_text     = excluded.normalized_text,
                is_answer           = admin_messages.is_answer or excluded.is_answer,
                reply_to_message_id = coalesce(excluded.reply_to_message_id,
                                               admin_messages.reply_to_message_id),
                embedding           = coalesce(excluded.embedding, admin_messages.embedding)
            returning id
            """,
            {
                "message_id": int(message_id),
                "chat_id": int(chat_id),
                "admin_id": int(admin_id),
                "username": admin_username,
                "text": clean,
                "normalized": normalize_text(clean),
                "message_type": message_type,
                "reply_to": int(reply_to_message_id) if reply_to_message_id else None,
                "topic": classify_topic(clean),
                "keywords": extract_keywords(clean, max_keywords=20),
                "crypto": extract_crypto_terms(clean),
                "language": _detect_language(clean),
                "is_answer": bool(is_answer),
                "ts": ts,
                "embedding": _vector_param(embedding),
            },
        )
        if row:
            bump_memory_generation()
            return int(row["id"])
        return None
    except Exception as exc:
        logger.error("save_admin_message failed: %s", redact(str(exc))[:250])
        return None


def mark_admin_message_as_answer(chat_id: int, message_id: int) -> None:
    try:
        _execute(
            """
            update public.admin_messages set is_answer = true
            where chat_id = %(chat_id)s and telegram_message_id = %(message_id)s
            """,
            {"chat_id": int(chat_id), "message_id": int(message_id)},
        )
    except Exception as exc:
        logger.debug("mark_admin_message_as_answer failed: %s", exc)


def export_admin_messages(limit: int = 1000, chat_id: int | None = None) -> list[dict[str, Any]]:
    """Newest admin messages of one chat (or of every chat) for exports/backups."""
    limit = max(1, min(int(limit), 20000))
    if chat_id is None:
        return _fetchall(
            """
            select id, chat_id, telegram_message_id, admin_id, admin_username, message_text,
                   topic, is_answer, message_timestamp, created_at
            from public.admin_messages
            order by message_timestamp desc
            limit %(limit)s
            """,
            {"limit": limit},
        )
    return _fetchall(
        """
        select id, chat_id, telegram_message_id, admin_id, admin_username, message_text,
               topic, is_answer, message_timestamp, created_at
        from public.admin_messages
        where chat_id = %(chat_id)s
        order by message_timestamp desc
        limit %(limit)s
        """,
        {"chat_id": chat_id, "limit": limit},
    )


def export_qa_pairs(limit: int = 1000, chat_id: int | None = None) -> list[dict[str, Any]]:
    """Newest Q&A memories of one chat (or of every chat) for exports/backups."""
    limit = max(1, min(int(limit), 20000))
    if chat_id is None:
        return _fetchall(
            """
            select id, chat_id, member_user_id, member_username, question_message_id,
                   question_text, admin_message_id, admin_id, admin_answer_text, topic,
                   pair_method, usage_count, question_timestamp, answer_timestamp,
                   "timestamp", created_at
            from public.qa_memory
            order by answer_timestamp desc
            limit %(limit)s
            """,
            {"limit": limit},
        )
    return _fetchall(
        """
        select id, chat_id, member_user_id, member_username, question_message_id,
               question_text, admin_message_id, admin_id, admin_answer_text, topic,
               pair_method, usage_count, question_timestamp, answer_timestamp,
               "timestamp", created_at
        from public.qa_memory
        where chat_id = %(chat_id)s
        order by answer_timestamp desc
        limit %(limit)s
        """,
        {"chat_id": chat_id, "limit": limit},
    )


def count_admin_messages(chat_id: int | None = None) -> int:
    if chat_id is None:
        row = _fetchone("select count(*) as c from public.admin_messages")
    else:
        row = _fetchone(
            "select count(*) as c from public.admin_messages where chat_id = %(chat_id)s",
            {"chat_id": int(chat_id)},
        )
    return int(row["c"]) if row else 0


def _import_id(value: str) -> int:
    """Stable negative Telegram-like id reserved for imported records."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:15]
    return -int(digest, 16)


def import_admin_memory(records: Sequence[Mapping[str, Any]], *, chat_id: int,
                        admin_id: int, source: str = "manual import") -> dict[str, int]:
    """Import memory rows atomically; existing content is never overwritten."""
    result = {"imported": 0, "duplicates": 0, "failed": 0}
    with get_connection() as conn:
        with conn.cursor() as cur:
            for record in records:
                text = str(record.get("text") or record.get("message") or "").strip()
                if not text:
                    result["failed"] += 1
                    continue
                fingerprint = hashlib.sha256(
                    f"{chat_id}|{admin_id}|{text}".encode("utf-8")).hexdigest()
                message_id = _import_id(f"memory:{fingerprint}")
                cur.execute(
                    """
                    insert into public.admin_messages
                      (telegram_message_id, chat_id, admin_id, admin_username,
                       message_text, normalized_text, message_type, topic, keywords,
                       crypto_terms, language, message_timestamp)
                    values (%(message_id)s, %(chat_id)s, %(admin_id)s, %(username)s,
                            %(text)s, %(normalized)s, %(message_type)s, %(topic)s,
                            %(keywords)s, %(crypto)s, %(language)s, %(timestamp)s)
                    on conflict (chat_id, telegram_message_id) do nothing
                    """,
                    {"message_id": message_id, "chat_id": int(chat_id),
                     "admin_id": int(admin_id), "username": record.get("username"),
                     "text": text, "normalized": normalize_text(text),
                     "message_type": f"imported:{str(record.get('source') or source)[:80]}",
                     "topic": classify_topic(text), "keywords": extract_keywords(text, max_keywords=20),
                     "crypto": extract_crypto_terms(text), "language": _detect_language(text),
                     "timestamp": record.get("date") or datetime.now(timezone.utc)})
                if cur.rowcount:
                    result["imported"] += 1
                else:
                    result["duplicates"] += 1
    if result["imported"]:
        bump_memory_generation()
    return result


def import_qa_memory(records: Sequence[Mapping[str, Any]], *, chat_id: int,
                     admin_id: int, source: str = "manual import") -> dict[str, int]:
    """Import Q&A rows atomically using stable ids and the existing schema."""
    result = {"imported": 0, "duplicates": 0, "failed": 0}
    with get_connection() as conn:
        with conn.cursor() as cur:
            for record in records:
                question = str(record.get("question") or "").strip()
                answer = str(record.get("answer") or "").strip()
                if not question or not answer:
                    result["failed"] += 1
                    continue
                fingerprint = hashlib.sha256(
                    f"{chat_id}|{question}|{answer}".encode("utf-8")).hexdigest()
                q_id = _import_id(f"qa-question:{fingerprint}")
                a_id = _import_id(f"qa-answer:{fingerprint}")
                ts = record.get("date") or datetime.now(timezone.utc)
                combined = f"{question} {answer}"
                cur.execute(
                    """
                    insert into public.qa_memory
                      (chat_id, member_user_id, member_username, question_message_id,
                       question_text, normalized_question, admin_message_id, admin_id,
                       admin_answer_text, normalized_answer, pair_method,
                       pair_confidence, topic, keywords, crypto_terms, language,
                       question_timestamp, answer_timestamp)
                    values (%(chat_id)s, 0, null, %(q_id)s, %(question)s, %(q_norm)s,
                            %(a_id)s, %(admin_id)s, %(answer)s, %(a_norm)s, 'manual',
                            1.0, %(topic)s, %(keywords)s, %(crypto)s, %(language)s,
                            %(timestamp)s, %(timestamp)s)
                    on conflict (chat_id, question_message_id) do nothing
                    """,
                    {"chat_id": int(chat_id), "q_id": q_id, "question": question,
                     "q_norm": normalize_text(question), "a_id": a_id,
                     "admin_id": int(admin_id), "answer": answer,
                     "a_norm": normalize_text(answer), "topic": classify_topic(combined),
                     "keywords": extract_keywords(combined, max_keywords=25),
                     "crypto": extract_crypto_terms(combined), "language": _detect_language(question),
                     "timestamp": ts})
                if cur.rowcount:
                    result["imported"] += 1
                else:
                    result["duplicates"] += 1
    if result["imported"]:
        bump_memory_generation()
    return result


def recent_admin_messages(chat_id: int, limit: int = 10,
                          admin_id: int | None = None) -> list[dict[str, Any]]:
    conditions = ["chat_id = %(chat_id)s"]
    params: dict[str, Any] = {"chat_id": int(chat_id), "limit": max(1, min(limit, 100))}
    if admin_id:
        conditions.append("admin_id = %(admin_id)s")
        params["admin_id"] = int(admin_id)
    return _fetchall(
        f"""
        select id, telegram_message_id, admin_id, admin_username, message_text, topic,
               keywords, crypto_terms, is_answer, message_timestamp, created_at, embedding
        from public.admin_messages
        where {' and '.join(conditions)}
        order by message_timestamp desc
        limit %(limit)s
        """,
        params,
    )


def search_admin_messages(*, chat_id: int, query_text: str, keywords: Sequence[str] = (),
                          limit: int = 200) -> list[dict[str, Any]]:
    """Candidate fetch for the retrieval engine (admin memory).

    Strategy: full-text search UNION keyword overlap UNION (optional) trigram
    similarity, all inside one query, ordered by best rank then recency.
    """
    params: dict[str, Any] = {
        "chat_id": int(chat_id),
        "q": (query_text or "")[:2000],
        "needle": _ilike_pattern(query_text),
        "limit": max(1, min(int(limit), 2000)),
    }
    conditions = []
    selects = ["coalesce(ts_rank(m.search_vector, plainto_tsquery('simple', %(q)s)), 0) as fts_rank"]

    conditions.append("m.search_vector @@ plainto_tsquery('simple', %(q)s)")
    if keywords:
        params["keywords"] = list(dict.fromkeys(keywords))[:30]
        conditions.append("m.keywords && %(keywords)s::text[]")
    if params["needle"]:
        conditions.append("m.message_text ilike %(needle)s")
    if _has_trgm and len(params["q"]) >= 4:
        conditions.append("similarity(m.message_text, %(q)s) > 0.12")

    sql = f"""
        select m.id, m.telegram_message_id, m.chat_id, m.admin_id, m.admin_username,
               m.message_text, m.topic, m.keywords, m.crypto_terms, m.is_answer,
               m.message_timestamp, m.created_at, m.embedding,
               {', '.join(selects)}
        from public.admin_messages m
        where m.chat_id = %(chat_id)s
          and ({' or '.join(conditions)})
        order by fts_rank desc, m.message_timestamp desc
        limit %(limit)s
    """
    try:
        rows = _fetchall(sql, params)
    except Exception as exc:
        logger.warning("search_admin_messages failed, falling back to recent: %s",
                       redact(str(exc))[:200])
        rows = recent_admin_messages(chat_id, limit=min(50, params["limit"]))

    if len(rows) < max(5, limit // 10):
        # add a recency-anchored tail so cold starts still have context
        try:
            tail = recent_admin_messages(chat_id, limit=min(30, params["limit"]))
            seen = {row.get("id") for row in rows}
            rows.extend(row for row in tail if row.get("id") not in seen)
        except Exception:
            pass
    return rows


# --------------------------------------------------------------------------- #
# pending questions
# --------------------------------------------------------------------------- #
def create_pending_question(*, chat_id: int, message_id: int, user_id: int,
                            text: str, message_timestamp: datetime | None = None,
                            username: str | None = None, is_question: bool = True,
                            ttl_seconds: int = 1800) -> int | None:
    clean = (text or "").strip()
    if not clean:
        return None
    ts = message_timestamp or datetime.now(timezone.utc)
    expires = ts + timedelta(seconds=max(60, int(ttl_seconds)))
    try:
        row = _fetchone(
            """
            insert into public.pending_questions (
                chat_id, telegram_message_id, member_user_id, member_username,
                question_text, normalized_text, keywords, crypto_terms, topic,
                is_question, message_timestamp, expires_at
            ) values (
                %(chat_id)s, %(message_id)s, %(user_id)s, %(username)s,
                %(text)s, %(normalized)s, %(keywords)s, %(crypto)s, %(topic)s,
                %(is_question)s, %(ts)s, %(expires)s
            )
            on conflict (chat_id, telegram_message_id) do update set
                question_text = excluded.question_text,
                is_question   = excluded.is_question
            returning id
            """,
            {
                "chat_id": int(chat_id),
                "message_id": int(message_id),
                "user_id": int(user_id),
                "username": username,
                "text": clean,
                "normalized": normalize_text(clean),
                "keywords": extract_keywords(clean, max_keywords=20),
                "crypto": extract_crypto_terms(clean),
                "topic": classify_topic(clean),
                "is_question": bool(is_question),
                "ts": ts,
                "expires": expires,
            },
        )
        return int(row["id"]) if row else None
    except Exception as exc:
        logger.warning("create_pending_question failed: %s", redact(str(exc))[:200])
        return None


def find_pending_by_reply(chat_id: int, reply_to_message_id: int) -> dict[str, Any] | None:
    return _fetchone(
        """
        select * from public.pending_questions
        where chat_id = %(chat_id)s and telegram_message_id = %(message_id)s
        order by message_timestamp desc
        limit 1
        """,
        {"chat_id": int(chat_id), "message_id": int(reply_to_message_id)},
    )


def find_open_pending_questions(chat_id: int, *, window_seconds: int = 300,
                                admin_id: int | None = None,
                                only_questions: bool = False,
                                limit: int = 5) -> list[dict[str, Any]]:
    """Unanswered member messages inside the pairing window, best candidate first."""
    conditions = [
        "p.chat_id = %(chat_id)s",
        "p.paired = false",
        "p.message_timestamp >= now() - make_interval(secs => %(window)s)",
        "p.expires_at > now()",
    ]
    params: dict[str, Any] = {
        "chat_id": int(chat_id),
        "window": max(30, int(window_seconds)),
        "limit": max(1, min(int(limit), 20)),
    }
    if admin_id:
        conditions.append("p.member_user_id <> %(admin_id)s")
        params["admin_id"] = int(admin_id)
    if only_questions:
        conditions.append("p.is_question = true")

    return _fetchall(
        f"""
        select p.*, extract(epoch from (now() - p.message_timestamp)) as age_seconds
        from public.pending_questions p
        where {' and '.join(conditions)}
        order by p.is_question desc, p.message_timestamp desc
        limit %(limit)s
        """,
        params,
    )


def mark_pending_paired(pending_id: int, qa_id: int | None) -> None:
    try:
        _execute(
            """
            update public.pending_questions
               set paired = true, paired_qa_id = %(qa_id)s
             where id = %(id)s
            """,
            {"qa_id": int(qa_id) if qa_id else None, "id": int(pending_id)},
        )
    except Exception as exc:
        logger.debug("mark_pending_paired failed: %s", exc)


def mark_pending_paired_by_message(chat_id: int, message_id: int, qa_id: int | None) -> None:
    """Pair the pending row identified by the *question* message id."""
    try:
        _execute(
            """
            update public.pending_questions
               set paired = true, paired_qa_id = %(qa_id)s
             where chat_id = %(chat_id)s and telegram_message_id = %(message_id)s
            """,
            {"qa_id": int(qa_id) if qa_id else None,
             "chat_id": int(chat_id), "message_id": int(message_id)},
        )
    except Exception as exc:
        logger.debug("mark_pending_paired_by_message failed: %s", exc)


def expire_pending_questions() -> int:
    try:
        return _execute(
            "update public.pending_questions set paired = true "
            "where paired = false and expires_at < now()"
        )
    except Exception as exc:
        logger.debug("expire_pending_questions failed: %s", exc)
        return 0


def count_open_questions(chat_id: int | None = None) -> int:
    if chat_id is None:
        row = _fetchone("select count(*) as c from public.pending_questions where paired = false")
    else:
        row = _fetchone(
            "select count(*) as c from public.pending_questions "
            "where paired = false and chat_id = %(chat_id)s",
            {"chat_id": int(chat_id)},
        )
    return int(row["c"]) if row else 0


# --------------------------------------------------------------------------- #
# qa memory
# --------------------------------------------------------------------------- #
def save_qa_memory(*, chat_id: int, member_user_id: int, member_username: str | None,
                   question_message_id: int, question_text: str,
                   admin_message_id: int, admin_id: int, admin_answer_text: str,
                   question_timestamp: datetime | None = None,
                   answer_timestamp: datetime | None = None,
                   pair_method: str = "reply", pair_confidence: float = 0.95,
                   is_question: bool = True,
                   embedding: Sequence[float] | None = None) -> int | None:
    """Persist a verified question/answer pair. Returns the row id."""
    q_text = (question_text or "").strip()
    a_text = (admin_answer_text or "").strip()
    if not q_text or not a_text:
        return None
    q_ts = question_timestamp or datetime.now(timezone.utc)
    a_ts = answer_timestamp or q_ts
    if pair_method not in {"reply", "window", "manual"}:
        pair_method = "window"
    combined = f"{q_text} {a_text}"
    try:
        row = _fetchone(
            """
            insert into public.qa_memory (
                chat_id, member_user_id, member_username, question_message_id, question_text,
                normalized_question, admin_message_id, admin_id, admin_answer_text,
                normalized_answer, pair_method, pair_confidence, is_question, topic, keywords,
                crypto_terms, language, question_timestamp, answer_timestamp, embedding
            ) values (
                %(chat_id)s, %(member_user_id)s, %(member_username)s, %(q_id)s, %(q_text)s,
                %(q_norm)s, %(a_id)s, %(admin_id)s, %(a_text)s,
                %(a_norm)s, %(pair_method)s, %(confidence)s, %(is_question)s, %(topic)s,
                %(keywords)s, %(crypto)s, %(language)s, %(q_ts)s, %(a_ts)s, %(embedding)s
            )
            on conflict (chat_id, question_message_id) do nothing
            returning id
            """,
            {
                "chat_id": int(chat_id),
                "member_user_id": int(member_user_id),
                "member_username": member_username,
                "q_id": int(question_message_id),
                "q_text": q_text,
                "q_norm": normalize_text(q_text),
                "a_id": int(admin_message_id),
                "admin_id": int(admin_id),
                "a_text": a_text,
                "a_norm": normalize_text(a_text),
                "pair_method": pair_method,
                "confidence": float(pair_confidence),
                "is_question": bool(is_question),
                "topic": classify_topic(combined),
                "keywords": extract_keywords(combined, max_keywords=25),
                "crypto": extract_crypto_terms(combined),
                "language": _detect_language(q_text),
                "q_ts": q_ts,
                "a_ts": a_ts,
                "embedding": _vector_param(embedding),
            },
        )
        if row:
            bump_memory_generation()
            return int(row["id"])
        existing = _fetchone(
            "select id from public.qa_memory where chat_id = %(chat_id)s "
            "and question_message_id = %(q_id)s",
            {"chat_id": int(chat_id), "q_id": int(question_message_id)},
        )
        return int(existing["id"]) if existing else None
    except Exception as exc:
        logger.error("save_qa_memory failed: %s", redact(str(exc))[:250])
        return None


def search_qa_memory(*, chat_id: int, query_text: str, keywords: Sequence[str] = (),
                     limit: int = 200) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "chat_id": int(chat_id),
        "q": (query_text or "")[:2000],
        "needle": _ilike_pattern(query_text),
        "limit": max(1, min(int(limit), 2000)),
    }
    conditions = ["q.question_search @@ plainto_tsquery('simple', %(q)s)"]
    if keywords:
        params["keywords"] = list(dict.fromkeys(keywords))[:30]
        conditions.append("q.keywords && %(keywords)s::text[]")
    if params["needle"]:
        conditions.append("(q.question_text ilike %(needle)s or q.admin_answer_text ilike %(needle)s)")
    if _has_trgm and len(params["q"]) >= 4:
        conditions.append(
            "(similarity(q.question_text, %(q)s) > 0.12 or similarity(q.admin_answer_text, %(q)s) > 0.12)"
        )

    sql = f"""
        select q.id, q.chat_id, q.member_user_id, q.member_username, q.question_message_id,
               q.question_text, q.admin_message_id, q.admin_id, q.admin_answer_text,
               q.pair_method, q.pair_confidence, q.is_question, q.topic, q.keywords,
               q.crypto_terms, q.language, q.question_timestamp, q.answer_timestamp,
               q.usage_count, q.embedding,
               coalesce(ts_rank(q.question_search, plainto_tsquery('simple', %(q)s)), 0) as fts_rank
        from public.qa_memory q
        where q.chat_id = %(chat_id)s
          and ({' or '.join(conditions)})
        order by fts_rank desc, q.answer_timestamp desc
        limit %(limit)s
    """
    try:
        rows = _fetchall(sql, params)
    except Exception as exc:
        logger.warning("search_qa_memory failed, falling back to recent: %s",
                       redact(str(exc))[:200])
        rows = recent_qa_pairs(chat_id, limit=min(50, params["limit"]))

    if len(rows) < max(3, limit // 20):
        try:
            tail = recent_qa_pairs(chat_id, limit=min(20, params["limit"]))
            seen = {row.get("id") for row in rows}
            rows.extend(row for row in tail if row.get("id") not in seen)
        except Exception:
            pass
    return rows


def recent_qa_pairs(chat_id: int, limit: int = 10) -> list[dict[str, Any]]:
    return _fetchall(
        """
        select q.id, q.chat_id, q.member_user_id, q.member_username, q.question_message_id,
               q.question_text, q.admin_message_id, q.admin_id, q.admin_answer_text,
               q.pair_method, q.pair_confidence, q.is_question, q.topic, q.keywords,
               q.crypto_terms, q.language, q.question_timestamp, q.answer_timestamp,
               q.usage_count, q.embedding
        from public.qa_memory q
        where q.chat_id = %(chat_id)s
        order by q.answer_timestamp desc
        limit %(limit)s
        """,
        {"chat_id": int(chat_id), "limit": max(1, min(limit, 100))},
    )


def get_recent_memories(chat_id: int, limit: int = 8) -> list[dict[str, Any]]:
    """Newest admin messages and Q&A pairs merged (cold-start fallback)."""
    rows: list[dict[str, Any]] = []
    try:
        for row in recent_qa_pairs(chat_id, limit=limit):
            row = dict(row)
            row["kind"] = "qa"
            rows.append(row)
    except Exception as exc:
        logger.debug("get_recent_memories(qa) failed: %s", exc)
    try:
        for row in recent_admin_messages(chat_id, limit=limit):
            row = dict(row)
            row["kind"] = "admin"
            rows.append(row)
    except Exception as exc:
        logger.debug("get_recent_memories(admin) failed: %s", exc)
    return rows


def record_memory_usage(pair_ids: Iterable[int]) -> None:
    """Bump the ``usage_count`` of Q&A pairs that were actually used."""
    try:
        qa_ids = [int(i) for i in pair_ids][:50]
        if qa_ids:
            _execute(
                "update public.qa_memory set usage_count = usage_count + 1, "
                "last_used_at = now() where id = any(%(ids)s::bigint[])",
                {"ids": qa_ids},
            )
    except Exception as exc:
        logger.debug("record_memory_usage failed: %s", exc)


def count_qa_pairs(chat_id: int | None = None) -> int:
    if chat_id is None:
        row = _fetchone("select count(*) as c from public.qa_memory")
    else:
        row = _fetchone("select count(*) as c from public.qa_memory where chat_id = %(chat_id)s",
                        {"chat_id": int(chat_id)})
    return int(row["c"]) if row else 0


def count_questions_asked() -> int:
    row = _fetchone("select count(*) as c from public.pending_questions where is_question = true")
    return int(row["c"]) if row else 0


def count_unanswered_questions() -> int:
    row = _fetchone(
        "select count(*) as c from public.message_logs "
        "where is_question = true and answered_by_admin = false"
    )
    return int(row["c"]) if row else 0


def latest_memory_date() -> datetime | None:
    row = _fetchone(
        """
        select greatest(
            coalesce((select max(message_timestamp) from public.admin_messages), 'epoch'::timestamptz),
            coalesce((select max(answer_timestamp) from public.qa_memory), 'epoch'::timestamptz)
        ) as latest
        """
    )
    if not row or row.get("latest") is None:
        return None
    value = row["latest"]
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    # `epoch` is the coalesce() fallback for "no memory yet"
    if value <= datetime(1970, 1, 2, tzinfo=timezone.utc):
        return None
    return value


def topic_breakdown(limit: int = 8) -> list[dict[str, Any]]:
    return _fetchall(
        """
        select topic, count(*) as total
        from public.qa_memory
        group by topic
        order by total desc
        limit %(limit)s
        """,
        {"limit": max(1, min(limit, 30))},
    )


# --------------------------------------------------------------------------- #
# message logs (analytics only -- never AI memory)
# --------------------------------------------------------------------------- #
def log_message(*, chat_id: int, message_id: int, user_id: int, username: str | None,
                text: str | None, message_timestamp: datetime | None = None,
                is_target_admin: bool = False, is_bot: bool = False,
                is_question: bool = False, answered_by_admin: bool = False,
                message_type: str = "text",
                purpose: str = "analytics",
                reply_to_message_id: int | None = None,
                max_chars: int = 600) -> bool:
    clean = (text or "").strip()
    stored = truncate_text(clean, max_chars) if max_chars else clean[:1000]
    ts = message_timestamp or datetime.now(timezone.utc)
    if purpose not in {"analytics", "unanswered_question"}:
        purpose = "analytics"
    try:
        _execute(
            """
            insert into public.message_logs (
                chat_id, telegram_message_id, user_id, username, is_target_admin, is_bot,
                message_type, message_text, text_length, is_question, answered_by_admin,
                purpose, topic, reply_to_message_id, message_timestamp
            ) values (
                %(chat_id)s, %(message_id)s, %(user_id)s, %(username)s, %(is_admin)s, %(is_bot)s,
                %(message_type)s, %(text)s, %(length)s, %(is_question)s, %(answered)s,
                %(purpose)s, %(topic)s, %(reply_to)s, %(ts)s
            )
            on conflict (chat_id, telegram_message_id) do nothing
            """,
            {
                "chat_id": int(chat_id),
                "message_id": int(message_id),
                "user_id": int(user_id),
                "username": username,
                "is_admin": bool(is_target_admin),
                "is_bot": bool(is_bot),
                "message_type": message_type,
                "text": stored or None,
                "length": len(clean),
                "is_question": bool(is_question),
                "answered": bool(answered_by_admin),
                "purpose": purpose,
                "topic": classify_topic(clean),
                "reply_to": int(reply_to_message_id) if reply_to_message_id else None,
                "ts": ts,
            },
        )
        return True
    except Exception as exc:
        logger.debug("log_message failed: %s", exc)
        return False


def mark_question_answered(chat_id: int, message_id: int) -> None:
    try:
        _execute(
            """
            update public.message_logs set answered_by_admin = true
            where chat_id = %(chat_id)s and telegram_message_id = %(message_id)s
            """,
            {"chat_id": int(chat_id), "message_id": int(message_id)},
        )
    except Exception as exc:
        logger.debug("mark_question_answered failed: %s", exc)


def recent_unanswered_questions(limit: int = 10) -> list[dict[str, Any]]:
    return _fetchall(
        """
        select chat_id, telegram_message_id, user_id, username, message_text,
               message_timestamp
        from public.message_logs
        where is_question = true and answered_by_admin = false
        order by message_timestamp desc
        limit %(limit)s
        """,
        {"limit": max(1, min(limit, 50))},
    )


def purge_message_logs(retention_days: int = 30) -> int:
    try:
        return _execute(
            "delete from public.message_logs "
            "where message_timestamp < now() - make_interval(days => %(days)s)",
            {"days": int(retention_days)},
        )
    except Exception as exc:
        logger.debug("purge_message_logs failed: %s", exc)
        return 0


def count_message_logs() -> int:
    row = _fetchone("select count(*) as c from public.message_logs")
    return int(row["c"]) if row else 0


# --------------------------------------------------------------------------- #
# conversation sessions (short-term context)
# --------------------------------------------------------------------------- #
def get_session(chat_id: int, user_id: int) -> dict[str, Any] | None:
    try:
        row = _fetchone(
            """
            select * from public.conversation_sessions
            where chat_id = %(chat_id)s and user_id = %(user_id)s and expires_at > now()
            """,
            {"chat_id": int(chat_id), "user_id": int(user_id)},
        )
    except Exception as exc:
        logger.debug("get_session failed: %s", exc)
        return None
    if row and isinstance(row.get("history"), str):
        try:
            row["history"] = json.loads(row["history"])
        except Exception:
            row["history"] = []
    return row


def save_session(chat_id: int, user_id: int, history: Sequence[Mapping[str, str]],
                 ttl_seconds: int = 1800, last_question: str = "",
                 last_answer: str = "") -> None:
    payload = [
        {"role": str(turn.get("role", "user")), "content": truncate_text(str(turn.get("content", "")), 700)}
        for turn in list(history)[-8:]
    ]
    try:
        _execute(
            """
            insert into public.conversation_sessions (
                chat_id, user_id, history, turn_count, last_question, last_answer,
                expires_at, updated_at
            ) values (
                %(chat_id)s, %(user_id)s, %(history)s::jsonb, %(turns)s, %(last_q)s, %(last_a)s,
                now() + make_interval(secs => %(ttl)s), now()
            )
            on conflict (chat_id, user_id) do update set
                history       = excluded.history,
                turn_count    = excluded.turn_count,
                last_question = excluded.last_question,
                last_answer   = excluded.last_answer,
                expires_at    = excluded.expires_at,
                updated_at    = now()
            """,
            {
                "chat_id": int(chat_id),
                "user_id": int(user_id),
                "history": json.dumps(payload, ensure_ascii=False),
                "turns": len(payload),
                "last_q": truncate_text(last_question, 500),
                "last_a": truncate_text(last_answer, 1000),
                "ttl": int(ttl_seconds),
            },
        )
    except Exception as exc:
        logger.debug("save_session failed: %s", exc)


def purge_sessions() -> int:
    try:
        return _execute("delete from public.conversation_sessions where expires_at < now()")
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# response cache
# --------------------------------------------------------------------------- #
def get_cached_response(cache_key: str) -> dict[str, Any] | None:
    return _fetchone(
        """
        select cache_key, answer_text, model, used_memory, hits, created_at, expires_at
        from public.response_cache
        where cache_key = %(key)s and expires_at > now()
        """,
        {"key": cache_key},
    )


def save_cached_response(*, cache_key: str, chat_id: int | None, question_text: str,
                         answer_text: str, model: str = "", used_memory: bool = False,
                         ttl_seconds: int = 900) -> None:
    if not answer_text:
        return
    _execute(
        """
        insert into public.response_cache (
            cache_key, chat_id, question_text, answer_text, model, used_memory, expires_at
        ) values (
            %(key)s, %(chat_id)s, %(question)s, %(answer)s, %(model)s, %(used_memory)s,
            now() + make_interval(secs => %(ttl)s)
        )
        on conflict (cache_key) do update set
            answer_text = excluded.answer_text,
            model       = excluded.model,
            used_memory = excluded.used_memory,
            expires_at  = excluded.expires_at
        """,
        {
            "key": cache_key,
            "chat_id": int(chat_id) if chat_id else None,
            "question": truncate_text(question_text, 900),
            "answer": truncate_text(answer_text, 3500),
            "model": model or None,
            "used_memory": bool(used_memory),
            "ttl": max(60, int(ttl_seconds)),
        },
    )


def increment_cache_hit(cache_key: str) -> None:
    _execute(
        "update public.response_cache set hits = hits + 1 where cache_key = %(key)s",
        {"key": cache_key},
    )


def clear_response_cache(chat_id: int | None = None) -> int:
    try:
        if chat_id is None:
            return _execute("delete from public.response_cache")
        return _execute("delete from public.response_cache where chat_id = %(chat_id)s "
                        "or chat_id is null", {"chat_id": int(chat_id)})
    except Exception as exc:
        logger.debug("clear_response_cache failed: %s", exc)
        return 0


def count_cache_entries() -> int:
    row = _fetchone("select count(*) as c from public.response_cache")
    return int(row["c"]) if row else 0


# --------------------------------------------------------------------------- #
# ai usage log
# --------------------------------------------------------------------------- #
def log_ai_usage(*, chat_id: int | None = None, user_id: int | None = None,
                 model: str = "", status: str = "ok", error_type: str | None = None,
                 cached: bool = False, used_memory: bool = False,
                 prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0,
                 latency_ms: int = 0, memory_count: int = 0, qa_count: int = 0,
                 question_hash: str = "") -> None:
    try:
        _execute(
            """
            insert into public.ai_usage_log (
                chat_id, user_id, model, status, error_type, cached, used_memory,
                prompt_tokens, completion_tokens, total_tokens, latency_ms,
                memory_count, qa_count, question_hash
            ) values (
                %(chat_id)s, %(user_id)s, %(model)s, %(status)s, %(error_type)s, %(cached)s,
                %(used_memory)s, %(prompt_tokens)s, %(completion_tokens)s, %(total_tokens)s,
                %(latency_ms)s, %(memory_count)s, %(qa_count)s, %(question_hash)s
            )
            """,
            {
                "chat_id": int(chat_id) if chat_id else None,
                "user_id": int(user_id) if user_id else None,
                "model": model or None,
                "status": status,
                "error_type": error_type,
                "cached": bool(cached),
                "used_memory": bool(used_memory),
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "total_tokens": int(total_tokens or 0),
                "latency_ms": int(latency_ms or 0),
                "memory_count": int(memory_count or 0),
                "qa_count": int(qa_count or 0),
                "question_hash": question_hash or None,
            },
        )
    except Exception as exc:
        logger.debug("log_ai_usage failed: %s", exc)


def ai_usage_summary(hours: int = 24) -> dict[str, Any]:
    row = _fetchone(
        """
        select count(*) as calls,
               count(*) filter (where cached) as cached_calls,
               count(*) filter (where status <> 'ok') as failed_calls,
               coalesce(sum(total_tokens), 0) as tokens,
               coalesce(avg(latency_ms) filter (where status = 'ok'), 0) as avg_latency_ms
        from public.ai_usage_log
        where created_at > now() - make_interval(hours => %(hours)s)
        """,
        {"hours": int(hours)},
    ) or {}
    return {
        "calls": int(row.get("calls") or 0),
        "cached_calls": int(row.get("cached_calls") or 0),
        "failed_calls": int(row.get("failed_calls") or 0),
        "tokens": int(row.get("tokens") or 0),
        "avg_latency_ms": int(float(row.get("avg_latency_ms") or 0)),
    }


# --------------------------------------------------------------------------- #
# bot settings (runtime switches)
# --------------------------------------------------------------------------- #
def get_setting(key: str, default: str | None = None) -> str | None:
    try:
        row = _fetchone("select value from public.bot_settings where key = %(key)s",
                        {"key": key})
    except Exception as exc:
        logger.debug("get_setting(%s) failed: %s", key, exc)
        return default
    if not row:
        return default
    return row.get("value")


def get_all_settings() -> dict[str, str]:
    try:
        rows = _fetchall("select key, value, value_type, description from public.bot_settings")
    except Exception:
        return {}
    return {row["key"]: row["value"] for row in rows}


def set_setting(key: str, value: str, *, updated_by: int | None = None,
                value_type: str = "string") -> bool:
    try:
        _execute(
            """
            insert into public.bot_settings (key, value, value_type, updated_by, updated_at)
            values (%(key)s, %(value)s, %(type)s, %(by)s, now())
            on conflict (key) do update set
                value      = excluded.value,
                updated_by = excluded.updated_by,
                updated_at = now()
            """,
            {"key": key, "value": str(value), "type": value_type,
             "by": int(updated_by) if updated_by else None},
        )
        return True
    except Exception as exc:
        logger.warning("set_setting(%s) failed: %s", key, exc)
        return False


# --------------------------------------------------------------------------- #
# statistics & maintenance
# --------------------------------------------------------------------------- #
def get_stats(chat_id: int | None = None) -> dict[str, Any]:
    """Aggregated statistics for ``/stats`` and ``/memory_stats``."""
    stats: dict[str, Any] = {
        "admin_messages": 0,
        "qa_pairs": 0,
        "users": 0,
        "open_questions": 0,
        "unanswered_questions": 0,
        "message_logs": 0,
        "cache_entries": 0,
        "latest_memory": None,
        "topics": [],
        "ai_usage": {},
        "memory_generation": memory_generation,
        "error": "",
    }
    try:
        row = _fetchone(
            """
            select
              (select count(*) from public.admin_messages)              as admin_messages,
              (select count(*) from public.qa_memory)                   as qa_pairs,
              (select count(*) from public.bot_users)                   as users,
              (select count(*) from public.pending_questions where paired = false) as open_questions,
              (select count(*) from public.message_logs)                as message_logs,
              (select count(*) from public.response_cache)              as cache_entries
            """
        ) or {}
        stats.update({k: int(v or 0) for k, v in row.items()})
        stats["unanswered_questions"] = count_unanswered_questions()
        stats["latest_memory"] = latest_memory_date()
        stats["topics"] = topic_breakdown(limit=6)
        stats["ai_usage"] = ai_usage_summary(hours=24)
    except Exception as exc:
        stats["error"] = redact(str(exc))[:200]
        logger.warning("get_stats failed: %s", stats["error"])
    return stats


def clear_memory(scope: str, *, chat_id: int | None = None) -> dict[str, int]:
    """Delete stored knowledge. ``scope`` is one of :data:`CLEAR_SCOPES`."""
    scope = (scope or "").strip().lower()
    if scope not in CLEAR_SCOPES:
        raise ValueError(f"scope must be one of {', '.join(CLEAR_SCOPES)}")
    deleted = {"qa_memory": 0, "admin_messages": 0, "message_logs": 0,
               "pending_questions": 0, "response_cache": 0}

    def _delete(table: str, *, scoped: bool = True) -> int:
        where = " where chat_id = %(chat_id)s" if (scoped and chat_id) else ""
        return _execute(f"delete from public.{table}{where}",
                        {"chat_id": int(chat_id)} if (scoped and chat_id) else {})

    if scope in {"qa", "all"}:
        deleted["qa_memory"] = _delete("qa_memory")
        deleted["pending_questions"] = _delete("pending_questions")
    if scope in {"admin", "all"}:
        deleted["admin_messages"] = _delete("admin_messages")
    if scope in {"logs", "admin", "qa", "all"}:
        deleted["message_logs"] = _delete("message_logs")
    if scope in {"cache", "all"}:
        if chat_id:
            deleted["response_cache"] = clear_response_cache(chat_id)
        else:
            deleted["response_cache"] = _execute("delete from public.response_cache")
    bump_memory_generation()
    logger.warning("memory cleared scope=%s chat=%s deleted=%s", scope, chat_id, deleted)
    return deleted


def run_maintenance(*, log_retention_days: int = 30, cache_grace_minutes: int = 60,
                    chat_id: int | None = None) -> dict[str, int]:
    """Housekeeping: expire questions, purge old logs/sessions/cache entries."""
    result = {"expired_questions": 0, "purged_logs": 0, "purged_cache": 0, "purged_sessions": 0}
    try:
        row = _fetchone(
            "select * from public.bot_run_maintenance(%(days)s, %(grace)s)",
            {"days": int(log_retention_days), "grace": int(cache_grace_minutes)},
        )
        if row:
            result["expired_questions"] = int(row.get("expired_questions") or 0)
            result["purged_logs"] = int(row.get("purged_logs") or 0)
            result["purged_cache"] = int(row.get("purged_cache") or 0)
        result["purged_sessions"] = purge_sessions()
        logger.info("maintenance: %s", result)
    except Exception as exc:
        logger.warning("maintenance failed (is schema.sql applied?): %s",
                       redact(str(exc))[:200])
        result["error"] = 1  # type: ignore[assignment]
    return result


def vacuum_analyze(tables: Sequence[str] = ("admin_messages", "qa_memory", "message_logs")) -> None:
    """VACUUM cannot run inside a transaction: use a dedicated raw connection."""
    if psycopg2 is None:
        return
    try:
        conn = psycopg2.connect(_get_settings().database_url,
                                connect_timeout=_connect_timeout())
        conn.autocommit = True
        with conn.cursor() as cur:
            for table in tables:
                if not _SAFE_IDENTIFIER.match(table):
                    continue
                cur.execute(f"vacuum (analyze) public.{table}")
        conn.close()
        logger.info("vacuum analyze done for %s", ", ".join(tables))
    except Exception as exc:
        logger.debug("vacuum failed: %s", exc)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _ilike_pattern(query_text: str) -> str | None:
    """Build a safe ``%keyword%`` pattern from the most meaningful word."""
    text = (query_text or "").strip()
    if not text:
        return None
    words = [w for w in normalize_text(text).split() if len(w) >= 3]
    if not words:
        return None
    words.sort(key=len, reverse=True)
    needle = words[0][:24]
    return f"%{needle}%"


def _vector_param(vector: Sequence[float] | None) -> str | None:
    if not vector:
        return None
    try:
        return "{" + ",".join(f"{float(v):.6f}" for v in vector) + "}"
    except (TypeError, ValueError):
        return None


def _detect_language(text: str) -> str:
    from utils.text import detect_language
    return detect_language(text)

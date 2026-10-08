"""Answer caching (requirement #19, cost optimisation).

Two layers:

1. **In-memory TTL cache** -- instant, protects against the same question being
   asked several times in a minute inside a busy group.
2. **PostgreSQL ``response_cache`` table** -- survives restarts and cold starts
   on Render's free tier.

Cache keys include the question hash, the model and the "memory generation"
counter (see :mod:`database`), so memory changes invalidate old answers.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from config import Settings
from utils.helpers import TTLCache
from utils.logger import get_logger
from utils.text import normalize_text, sha256_short

logger = get_logger(__name__)


@dataclass
class CachedAnswer:
    text: str
    used_memory: bool
    model: str = ""
    source: str = "memory"  # "memory" | "database"
    created_at: float = 0.0


class AnswerCache:
    """Hierarchical cache for generated answers."""

    def __init__(self, settings: Settings, db: Any = None):
        self.settings = settings
        self.db = db
        self._local = TTLCache(ttl_seconds=max(60, settings.cache_ttl_seconds),
                               max_entries=max(50, settings.cache_max_entries))
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.db_hits = 0

    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return bool(self.settings.cache_enabled and self.settings.cache_ttl_seconds > 0)

    def make_key(self, chat_id: int, question: str, *, scope: str = "public") -> str:
        normalized = normalize_text(question)
        generation = 0
        if self.db is not None:
            try:
                generation = int(getattr(self.db, "memory_generation", 0) or 0)
            except Exception:
                generation = 0
        raw = f"{scope}|{chat_id}|{self.settings.groq_model}|{generation}|{normalized}"
        return sha256_short(raw, 32)

    # ------------------------------------------------------------------ #
    def get(self, chat_id: int, question: str, *, scope: str = "public") -> CachedAnswer | None:
        if not self.enabled:
            return None
        key = self.make_key(chat_id, question, scope=scope)

        local = self._local.get(key)
        if local is not None:
            with self._lock:
                self.hits += 1
            return CachedAnswer(text=local["text"], used_memory=bool(local.get("used_memory")),
                                model=local.get("model", ""), source="memory",
                                created_at=local.get("created_at", time.time()))

        if self.db is not None:
            try:
                row = self.db.get_cached_response(key)
            except Exception as exc:  # pragma: no cover - cache must never break the bot
                logger.debug("cache lookup failed: %s", exc)
                row = None
            if row:
                answer = CachedAnswer(
                    text=row.get("answer_text", ""),
                    used_memory=bool(row.get("used_memory")),
                    model=row.get("model") or "",
                    source="database",
                    created_at=time.time(),
                )
                if answer.text:
                    with self._lock:
                        self.hits += 1
                        self.db_hits += 1
                    self._local.set(key, {"text": answer.text,
                                          "used_memory": answer.used_memory,
                                          "model": answer.model,
                                          "created_at": time.time()})
                    try:
                        self.db.increment_cache_hit(key)
                    except Exception:
                        pass
                    return answer

        with self._lock:
            self.misses += 1
        return None

    # ------------------------------------------------------------------ #
    def set(self, chat_id: int, question: str, answer: str, *, used_memory: bool,
            model: str = "", scope: str = "public") -> None:
        if not self.enabled or not answer:
            return
        key = self.make_key(chat_id, question, scope=scope)
        self._local.set(key, {"text": answer, "used_memory": used_memory, "model": model,
                              "created_at": time.time()})
        if self.db is not None:
            try:
                self.db.save_cached_response(
                    cache_key=key,
                    chat_id=chat_id,
                    question_text=question[:1000],
                    answer_text=answer,
                    model=model,
                    used_memory=used_memory,
                    ttl_seconds=self.settings.cache_ttl_seconds,
                )
            except Exception as exc:  # pragma: no cover
                logger.debug("cache store failed: %s", exc)

    # ------------------------------------------------------------------ #
    def invalidate_chat(self, chat_id: int | None = None) -> int:
        """Drop cached answers (in memory). Returns the number of local entries removed."""
        size = len(self._local)
        self._local.clear()
        if self.db is not None:
            try:
                self.db.clear_response_cache(chat_id)
            except Exception as exc:  # pragma: no cover
                logger.warning("cache invalidation failed: %s", exc)
        return size

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        with self._lock:
            hits, misses, db_hits = self.hits, self.misses, self.db_hits
        return {
            "enabled": self.enabled,
            "entries": len(self._local),
            "hits": hits,
            "misses": misses,
            "db_hits": db_hits,
            "hit_rate": round(hits / total, 3) if total else 0.0,
        }

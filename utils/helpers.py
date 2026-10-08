"""Small runtime helpers: caches, rate limiting, formatting.

Everything in this module is intentionally in-memory and thread-safe, because
the Flask webhook worker handles updates on multiple threads while the Telegram
application runs its own event loop in a dedicated thread.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Hashable

TELEGRAM_TEXT_LIMIT = 4096
DHAKA_TZ = timezone(timedelta(hours=6))  # Asia/Dhaka (no DST)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_from_timestamp(ts: float | int | None) -> datetime:
    if ts is None:
        return now_utc()
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def human_time(value: datetime | None, tz: timezone = DHAKA_TZ) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(tz).strftime("%d %b %Y, %H:%M")


def human_count(value: Any) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "0"
    if number < 1000:
        return str(number)
    if number < 1_000_000:
        return f"{number / 1000:.1f}K".replace(".0K", "K")
    return f"{number / 1_000_000:.1f}M".replace(".0M", "M")


def mask_id(value: Any) -> str:
    """Mask a numeric Telegram id for logs (``123456789`` -> ``1234***89``)."""
    text = str(value or "")
    if len(text) <= 5:
        return "***"
    return f"{text[:4]}***{text[-2:]}"


def mask_secret(value: str | None, keep: int = 4) -> str:
    if not value:
        return "(not set)"
    text = str(value)
    if len(text) <= keep * 2:
        return "***"
    return f"{text[:keep]}…{text[-keep:]}"


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def chunk_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split long text on a sane boundary so Telegram accepts it."""
    if not text:
        return []
    text = str(text)
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        split_at = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "))
        if split_at < limit // 2:
            split_at = window.rfind(" ")
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    seconds = float(seconds)
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} m {sec} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} m"


# --------------------------------------------------------------------------- #
# in-memory caches
# --------------------------------------------------------------------------- #
class TTLCache:
    """Thread-safe cache with per-entry TTL and a size cap (oldest evicted)."""

    def __init__(self, ttl_seconds: float = 300, max_entries: int = 500):
        self.ttl = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._data: dict[Hashable, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Hashable, default: Any = None) -> Any:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if not item:
                return default
            expires_at, value = item
            if expires_at <= now:
                self._data.pop(key, None)
                return default
            return value

    def set(self, key: Hashable, value: Any, ttl: float | None = None) -> None:
        expires_at = time.time() + (self.ttl if ttl is None else float(ttl))
        with self._lock:
            self._data[key] = (expires_at, value)
            if len(self._data) > self.max_entries:
                # drop the entries closest to expiry
                for old_key, _ in sorted(self._data.items(), key=lambda kv: kv[1][0])[
                    : len(self._data) - self.max_entries
                ]:
                    self._data.pop(old_key, None)

    def pop(self, key: Hashable, default: Any = None) -> Any:
        with self._lock:
            item = self._data.pop(key, None)
        return item[1] if item else default

    def delete(self, key: Hashable) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def purge_expired(self) -> int:
        now = time.time()
        removed = 0
        with self._lock:
            for key in [k for k, (exp, _) in self._data.items() if exp <= now]:
                self._data.pop(key, None)
                removed += 1
        return removed

    def __contains__(self, key: Hashable) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def __len__(self) -> int:
        return len(self._data)


_MISSING = object()


class DedupeTracker:
    """Remembers recently seen ids (Telegram ``update_id``) to drop duplicates."""

    def __init__(self, ttl_seconds: float = 900, max_entries: int = 2000):
        self._cache = TTLCache(ttl_seconds, max_entries)

    def seen(self, key: Hashable) -> bool:
        """Return ``True`` if ``key`` was seen before; otherwise remember it."""
        if key is None:
            return False
        if self._cache.get(key) is not None:
            return True
        self._cache.set(key, True)
        return False

    def forget(self, key: Hashable) -> None:
        self._cache.delete(key)

    def __len__(self) -> int:
        return len(self._cache)


class RateLimiter:
    """Sliding-window per-key rate limiter (used for member questions)."""

    def __init__(self, limit: int = 6, window_seconds: float = 60.0):
        self.limit = int(limit)
        self.window = float(window_seconds)
        self._hits: dict[Hashable, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: Hashable) -> bool:
        now = time.time()
        with self._lock:
            queue = self._hits.setdefault(key, deque())
            while queue and now - queue[0] > self.window:
                queue.popleft()
            if len(queue) >= self.limit:
                return False
            queue.append(now)
            return True

    def remaining(self, key: Hashable) -> int:
        now = time.time()
        with self._lock:
            queue = self._hits.get(key)
            if not queue:
                return self.limit
            while queue and now - queue[0] > self.window:
                queue.popleft()
            return max(0, self.limit - len(queue))

    def retry_after(self, key: Hashable) -> float:
        now = time.time()
        with self._lock:
            queue = self._hits.get(key)
            if not queue or len(queue) < self.limit:
                return 0.0
            return max(0.0, self.window - (now - queue[0]))

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


class Throttle:
    """Run a callback at most once per interval (used for maintenance)."""

    def __init__(self, interval_seconds: float):
        self.interval = float(interval_seconds)
        self._last = 0.0
        self._lock = threading.Lock()

    def ready(self) -> bool:
        with self._lock:
            now = time.time()
            if now - self._last >= self.interval:
                self._last = now
                return True
            return False

    def mark(self) -> None:
        with self._lock:
            self._last = time.time()


class Once:
    """Thread-safe 'execute only once per process' guard."""

    def __init__(self, callback: Callable[[], Any] | None = None):
        self._done = False
        self._lock = threading.Lock()
        self._callback = callback

    def run(self) -> bool:
        with self._lock:
            if self._done:
                return False
            self._done = True
        if self._callback:
            self._callback()
        return True

    @property
    def done(self) -> bool:
        return self._done

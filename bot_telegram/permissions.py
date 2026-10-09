"""Authorization, trigger detection and group-administration caching.

Permission model
----------------
* **Target admin** (``TARGET_ADMIN_ID``): whose messages become memory.
* **Bot admins** (``ADMIN_ID``): may run admin commands (``/stats``, ``/clear_memory`` ...).
  By default the target admin is also a bot admin.
* **Group admins**: optionally trusted (``ALLOW_GROUP_ADMINS=true``) but only
  inside the configured group.
* Everybody else is a **member**: can ask questions, cannot see statistics or
  modify memory.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from config import Settings
from utils.logger import get_logger
from utils.text import normalize_text, sanitize_incoming_text

logger = get_logger(__name__)

GROUP_CHAT_TYPES = {"group", "supergroup"}
ADMIN_CACHE_TTL = 600.0


@dataclass
class Actor:
    """Who is doing something, in which chat."""

    user_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    chat_id: int | None = None
    chat_type: str = "private"
    is_bot: bool = False
    is_target_admin: bool = False
    is_bot_admin: bool = False
    is_group_admin: bool = False


class Permissions:
    """Central place for every authorization decision."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._group_admins: dict[int, set[int]] = {}
        self._group_admin_fetched: dict[int, float] = {}
        self._membership_cache: dict[tuple[int, int], tuple[bool, float]] = {}

    # ------------------------------------------------------------------ #
    # identity helpers
    # ------------------------------------------------------------------ #
    def actor_from(self, update: Any, chat: Any = None) -> Actor:
        user = getattr(update, "effective_user", None)
        chat = chat or getattr(update, "effective_chat", None)
        user_id = getattr(user, "id", None)
        chat_id = getattr(chat, "id", None)
        chat_type = getattr(chat, "type", "private") or "private"
        return Actor(
            user_id=user_id,
            username=getattr(user, "username", None),
            first_name=getattr(user, "first_name", None),
            chat_id=chat_id,
            chat_type=chat_type,
            is_bot=bool(getattr(user, "is_bot", False)),
            is_target_admin=self.settings.is_target_admin(user_id),
            is_bot_admin=self.settings.is_bot_admin(user_id),
            is_group_admin=self.is_group_admin(user_id, chat_id),
        )

    def is_target_admin(self, user_id: int | None) -> bool:
        return self.settings.is_target_admin(user_id)

    def is_bot_admin(self, user_id: int | None) -> bool:
        if self.settings.is_bot_admin(user_id):
            return True
        # the target admin is an admin of this bot by default
        return self.settings.is_target_admin(user_id)

    def in_target_group(self, chat_id: int | None) -> bool:
        """Return true only for the explicitly configured group."""
        return self.settings.group_id is not None and chat_id == self.settings.group_id

    async def is_authorized_member(self, bot: Any, chat_id: int | None,
                                   user_id: int | None, *, force: bool = False) -> bool:
        """Verify that a user currently belongs to the configured group."""
        if not self.in_target_group(chat_id) or user_id is None:
            return False
        key = (int(chat_id), int(user_id))
        cached = self._membership_cache.get(key)
        if not force and cached and time.time() - cached[1] < 60.0:
            return cached[0]
        allowed = False
        try:
            member = await bot.get_chat_member(int(chat_id), int(user_id))
            status = str(getattr(member, "status", ""))
            allowed = status in {"creator", "administrator", "member"}
            if status == "restricted":
                allowed = bool(getattr(member, "is_member", False))
        except Exception as exc:
            logger.warning("group membership check failed for user=%s chat=%s: %s",
                           user_id, chat_id, str(exc)[:160])
        self._membership_cache[key] = (allowed, time.time())
        return allowed

    def is_group_admin(self, user_id: int | None, chat_id: int | None) -> bool:
        if user_id is None or chat_id is None:
            return False
        return int(user_id) in self._group_admins.get(int(chat_id), set())

    # ------------------------------------------------------------------ #
    # group admin cache (Telegram API call, refreshed lazily)
    # ------------------------------------------------------------------ #
    async def refresh_group_admins(self, bot: Any, chat_id: int,
                                   *, force: bool = False) -> set[int]:
        if not self.settings.allow_group_admins:
            return set()
        fetched = self._group_admin_fetched.get(chat_id, 0.0)
        if not force and time.time() - fetched < ADMIN_CACHE_TTL:
            return self._group_admins.get(chat_id, set())
        try:
            admins = await bot.get_chat_administrators(chat_id)
            ids = {
                admin.user.id
                for admin in admins
                if getattr(admin, "user", None) and not getattr(admin.user, "is_bot", False)
            }
            self._group_admins[chat_id] = ids
            self._group_admin_fetched[chat_id] = time.time()
            logger.debug("cached %s group admins for chat %s", len(ids), chat_id)
            return ids
        except Exception as exc:
            logger.warning("could not fetch group administrators: %s", exc)
            self._group_admin_fetched[chat_id] = time.time()
            return self._group_admins.get(chat_id, set())

    def authorize_admin(self, actor: Actor) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for admin-only commands."""
        if actor.is_bot_admin:
            return True, "bot_admin"
        if (self.settings.allow_group_admins and actor.chat_type in GROUP_CHAT_TYPES
                and self.in_target_group(actor.chat_id) and actor.is_group_admin):
            return True, "group_admin"
        return False, "not_authorized"

    # ------------------------------------------------------------------ #
    # trigger detection
    # ------------------------------------------------------------------ #
    def bot_mention_tokens(self, bot_username: str | None) -> list[str]:
        tokens = []
        if bot_username:
            tokens.append(f"@{bot_username.lower()}")
        for name in self.settings.bot_name.split():
            if len(name) > 3:
                tokens.append(name.lower())
        return tokens

    def mentions_bot(self, text: str | None, bot_username: str | None) -> bool:
        if not text:
            return False
        lowered = text.lower()
        return any(token in lowered for token in self.bot_mention_tokens(bot_username))

    def is_reply_to_bot(self, message: Any, bot_id: int | None) -> bool:
        reply = getattr(message, "reply_to_message", None)
        if not reply or bot_id is None:
            return False
        author = getattr(reply, "from_user", None)
        return bool(author and getattr(author, "id", None) == bot_id)

    def matches_trigger_keyword(self, text: str | None) -> str | None:
        if not text or not self.settings.trigger_keywords:
            return None
        lowered = sanitize_incoming_text(text).lower()
        for keyword in self.settings.trigger_keywords:
            if keyword and keyword.lower() in lowered:
                return keyword
        return None

    def is_member_question(self, message: Any, user: Any, bot_id: int | None = None,
                           bot_username: str | None = None) -> tuple[bool, str]:
        """Should the bot answer this message? Returns ``(answer, reason)``."""
        text = getattr(message, "text", None) or getattr(message, "caption", None)
        if not text or not text.strip():
            return False, "no_text"
        if user is None or getattr(user, "is_bot", False):
            return False, "from_bot"
        if self.settings.is_target_admin(getattr(user, "id", None)):
            return False, "from_target_admin"
        if getattr(message, "chat", None) is not None:
            chat_type = getattr(message.chat, "type", "private")
            if chat_type in GROUP_CHAT_TYPES and not self.in_target_group(message.chat.id):
                return False, "other_group"

        if self.settings.trigger_on_reply and self.is_reply_to_bot(message, bot_id):
            return True, "reply_to_bot"
        if self.settings.trigger_on_mention and self.mentions_bot(text, bot_username):
            return True, "mention"
        keyword = self.matches_trigger_keyword(text)
        if keyword:
            return True, f"keyword:{keyword}"
        return False, "no_trigger"


def clean_question(text: str | None, bot_username: str | None = None,
                   limit: int = 1200) -> str:
    """Strip mentions/commands and trim a question to a sane length."""
    clean = sanitize_incoming_text(text, max_chars=limit * 2)
    if not clean:
        return ""
    if bot_username:
        lowered = clean.lower()
        token = f"@{bot_username.lower()}"
        while token in lowered:
            index = lowered.index(token)
            clean = clean[:index] + clean[index + len(token):]
            lowered = clean.lower()
    clean = clean.strip(" :,-–—\n\t")
    if len(clean) > limit:
        clean = clean[:limit].rstrip() + "…"
    return clean


def is_probably_question(text: str | None) -> bool:
    from utils.text import is_question
    return is_question(text)


def describe_actor(actor: Actor) -> str:
    parts = [f"user={actor.user_id}"]
    if actor.username:
        parts.append(f"@{actor.username}")
    parts.append(f"chat={actor.chat_id}({actor.chat_type})")
    roles = []
    if actor.is_target_admin:
        roles.append("target_admin")
    if actor.is_bot_admin:
        roles.append("bot_admin")
    if actor.is_group_admin:
        roles.append("group_admin")
    parts.append("roles=" + (",".join(roles) or "member"))
    return " ".join(parts)


def normalize_command(text: str | None) -> str:
    return normalize_text(text or "")


def split_command_args(text: str | None, command: str = "") -> str:
    """Return everything after the command name (``/ask BTC?`` -> ``BTC?``)."""
    clean = sanitize_incoming_text(text, max_chars=4000)
    if not clean:
        return ""
    if clean.startswith("/"):
        first, _, rest = clean.partition(" ")
        if rest:
            return rest.strip()
        return ""
    if command and clean.lower().startswith(command.lower()):
        return clean[len(command):].strip()
    return clean.strip()


def is_admin_username(user: Any, usernames: Iterable[str]) -> bool:
    username = (getattr(user, "username", None) or "").lower().lstrip("@")
    if not username:
        return False
    return username in {u.lower().lstrip("@") for u in usernames if u}

"""Message tracking and Q&A pairing -- the memory collector.

Two responsibilities (requirements #2, #4, #5, #7):

1. **Admin memory** -- every message written by ``TARGET_ADMIN_ID`` is stored in
   ``admin_messages``.
2. **Q&A pairing** -- when the admin answers a member question, both sides are
   stored in ``qa_memory``. Unanswered questions never reach ``qa_memory``; they
   stay in ``pending_questions``/``message_logs`` and expire.

Pairing priority (exactly as specified):

1. Telegram ``reply_to_message`` relationship (strongest evidence)
2. same conversation window with keyword/topic overlap
3. configurable time window (``QA_PAIR_WINDOW_SECONDS``)

False pairing guards:

* the reply target must be a *member* message, never another admin/bot message
* window pairing needs keyword, crypto-term or topic overlap, otherwise the
  question is skipped (unless a single very recent question exists)
* a pending question can only be paired once
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from config import Settings
from utils.logger import get_logger
from utils.text import (
    classify_topic,
    extract_crypto_terms,
    extract_keywords,
    is_question,
    sanitize_incoming_text,
    truncate,
)

from .permissions import GROUP_CHAT_TYPES

logger = get_logger(__name__)

#: a lone, very recent question may be paired without keyword overlap
LONE_QUESTION_MAX_AGE = 90.0

PAIR_CONFIDENCE = {
    "reply": 0.98,
    "reply_direct": 0.95,
    "window_overlap": 0.75,
    "window_lone": 0.62,
}

MIN_WINDOW_OVERLAP = 1  # at least one keyword / crypto term / topic in common


class MessageTracker:
    """Turns Telegram traffic into memory rows."""

    def __init__(self, db: Any, settings: Settings, *, embedder: Any = None,
                 cache: Any = None):
        self.db = db
        self.settings = settings
        self._embed = embedder
        self.cache = cache
        self.stats = {
            "admin_messages": 0,
            "questions": 0,
            "paired": 0,
            "skipped": 0,
            "errors": 0,
        }

    # ------------------------------------------------------------------ #
    # text helpers
    # ------------------------------------------------------------------ #
    def _message_text(self, message: Any) -> str:
        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        return sanitize_incoming_text(text, max_chars=6000)

    def _message_type(self, message: Any) -> str:
        for attr, name in (
            ("text", "text"),
            ("photo", "photo"),
            ("video", "video"),
            ("document", "document"),
            ("voice", "voice"),
            ("audio", "audio"),
            ("sticker", "sticker"),
            ("animation", "animation"),
        ):
            if getattr(message, attr, None) is not None:
                return name
        return "other"

    def _timestamp(self, message: Any) -> datetime:
        raw = getattr(message, "date", None)
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc)

    def _embedding(self, text: str) -> list[float] | None:
        if not self.settings.embedding_enabled or not text:
            return None
        if self._embed is None:
            from ai.embeddings import embed_text
            self._embed = embed_text
        try:
            return self._embed(text, self.settings.embedding_dim)
        except Exception as exc:  # pragma: no cover - embedding must never break tracking
            logger.debug("embedding failed: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    # admin memory
    # ------------------------------------------------------------------ #
    def track_admin_message(self, message: Any, user: Any) -> dict[str, Any]:
        """Store an admin message and try to pair it with a pending question."""
        result: dict[str, Any] = {"stored": False, "admin_message_id": None, "qa_id": None,
                                  "pair_method": None, "reason": ""}
        text = self._message_text(message)
        if not text:
            result["reason"] = "empty_text"
            return result

        chat_id = int(message.chat.id)
        chat_type = getattr(message.chat, "type", "private")
        admin_id = int(user.id)
        reply_to = getattr(message, "reply_to_message", None)
        reply_to_id = getattr(reply_to, "message_id", None)

        # 1. try to find the question this message answers -----------------
        pairing = None
        if chat_type in GROUP_CHAT_TYPES:
            pairing = self._resolve_pairing(message, user, admin_id, chat_id, text, reply_to)

        # 2. store the admin message --------------------------------------
        row_id = self.db.save_admin_message(
            chat_id=chat_id,
            message_id=int(message.message_id),
            admin_id=admin_id,
            admin_username=getattr(user, "username", None),
            text=text,
            message_timestamp=self._timestamp(message),
            reply_to_message_id=reply_to_id,
            message_type=self._message_type(message),
            is_answer=bool(pairing),
            embedding=self._embedding(text),
        )
        result["stored"] = row_id is not None
        result["admin_message_id"] = row_id
        if row_id:
            self.stats["admin_messages"] += 1
            logger.info("admin memory stored: admin=%s chat=%s len=%s pairing=%s",
                        admin_id, chat_id, len(text), bool(pairing))

        # 3. create the Q&A memory ----------------------------------------
        if pairing:
            qa_id = self._save_pair(
                admin_message=message, admin_id=admin_id, admin_username=getattr(user, "username", None),
                answer_text=text, answer_time=self._timestamp(message), pairing=pairing,
            )
            result["qa_id"] = qa_id
            result["pair_method"] = pairing["method"]
            if qa_id:
                self.stats["paired"] += 1
                logger.info("Q&A memory created: id=%s method=%s confidence=%s",
                            qa_id, pairing["method"], pairing["confidence"])
                if self.cache is not None:
                    self.cache.invalidate_chat(chat_id)
        return result

    # ------------------------------------------------------------------ #
    # member messages
    # ------------------------------------------------------------------ #
    def track_member_message(self, message: Any, user: Any) -> dict[str, Any]:
        """Log a member message and remember it as a candidate question."""
        text = self._message_text(message)
        chat_id = int(message.chat.id)
        question = is_question(text)
        result = {"question": question, "pending_id": None, "logged": False}

        if self.settings.log_unanswered_questions and text:
            result["logged"] = self.db.log_message(
                chat_id=chat_id,
                message_id=int(message.message_id),
                user_id=int(user.id),
                username=getattr(user, "username", None),
                text=text,
                message_timestamp=self._timestamp(message),
                is_target_admin=False,
                is_question=question,
                answered_by_admin=False,
                message_type=self._message_type(message),
                purpose="analytics",
                reply_to_message_id=getattr(getattr(message, "reply_to_message", None),
                                            "message_id", None),
                max_chars=self.settings.log_text_chars,
            )

        if question or self.settings.track_all_member_messages:
            pending_id = self.db.create_pending_question(
                chat_id=chat_id,
                message_id=int(message.message_id),
                user_id=int(user.id),
                username=getattr(user, "username", None),
                text=text,
                message_timestamp=self._timestamp(message),
                is_question=question,
                ttl_seconds=self.settings.pending_question_ttl_seconds,
            )
            result["pending_id"] = pending_id
            if question:
                self.stats["questions"] += 1
                logger.info("member question tracked: chat=%s user=%s pending=%s q=%s",
                            chat_id, getattr(user, "id", None), pending_id,
                            truncate(text, 80))
        return result

    # ------------------------------------------------------------------ #
    # pairing
    # ------------------------------------------------------------------ #
    def _resolve_pairing(self, message: Any, user: Any, admin_id: int, chat_id: int,
                         answer_text: str, reply_to: Any) -> dict[str, Any] | None:
        if reply_to is not None:
            pairing = self._pair_by_reply(message, admin_id, chat_id, reply_to)
            if pairing:
                return pairing
            # the admin answered somebody else (or another admin) -> no pairing
            return None
        if not self.settings.qa_pair_window_seconds:
            return None
        return self._pair_by_window(admin_id, chat_id, answer_text)

    def _pair_by_reply(self, message: Any, admin_id: int, chat_id: int,
                       reply_to: Any) -> dict[str, Any] | None:
        """Telegram reply relationship -- the most reliable signal."""
        author = getattr(reply_to, "from_user", None)
        if author is None:
            return None
        if getattr(author, "is_bot", False):
            return None
        # only *member* messages can be questions: replies to a configured admin
        # (target admin or bot admin) are conversations, not member questions.
        known_admins = set(self.settings.target_admin_ids) | set(self.settings.admin_ids)
        if int(getattr(author, "id", 0)) in known_admins:
            return None
        question_text = sanitize_incoming_text(
            (getattr(reply_to, "text", None) or getattr(reply_to, "caption", None) or ""),
            max_chars=4000,
        )
        if not question_text:
            return None

        reply_ts = self._timestamp(reply_to)
        answer_ts = self._timestamp(message)
        age = abs((answer_ts - reply_ts).total_seconds())
        if age > max(self.settings.qa_pair_window_seconds * 12, 86400):
            logger.debug("reply pairing skipped: reply too old (%ss)", int(age))
            return None

        pending = None
        try:
            pending = self.db.find_pending_by_reply(chat_id, int(reply_to.message_id))
        except Exception as exc:  # pragma: no cover
            logger.debug("find_pending_by_reply failed: %s", exc)

        already_paired = bool(pending and pending.get("paired"))
        if already_paired:
            logger.debug("reply pairing skipped: question %s already paired",
                         reply_to.message_id)
            return None

        return {
            "method": "reply",
            "confidence": PAIR_CONFIDENCE["reply"],
            "question_text": question_text,
            "question_message_id": int(reply_to.message_id),
            "question_timestamp": reply_ts,
            "member_user_id": int(author.id),
            "member_username": getattr(author, "username", None),
            "is_question": is_question(question_text),
            "pending_id": pending.get("id") if pending else None,
        }

    def _pair_by_window(self, admin_id: int, chat_id: int,
                        answer_text: str) -> dict[str, Any] | None:
        """Same-conversation window pairing with relevance guards."""
        try:
            candidates = self.db.find_open_pending_questions(
                chat_id,
                window_seconds=self.settings.qa_pair_window_seconds,
                admin_id=admin_id,
                only_questions=self.settings.qa_pair_require_question,
                limit=6,
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("find_open_pending_questions failed: %s", exc)
            return None
        if not candidates:
            return None

        answer_keywords = set(extract_keywords(answer_text, max_keywords=20))
        answer_crypto = set(extract_crypto_terms(answer_text))
        answer_topic = classify_topic(answer_text)

        best: tuple[float, dict[str, Any]] | None = None
        for candidate in candidates:
            question_text = candidate.get("question_text") or ""
            overlap = self._overlap(question_text, answer_keywords, answer_crypto, answer_topic)
            age = float(candidate.get("age_seconds") or self.settings.qa_pair_window_seconds)
            if overlap >= MIN_WINDOW_OVERLAP:
                # prefer newer, more relevant questions
                score = 1.0 + min(overlap, 5) * 0.2 - min(age, 600) / 3000.0
                method = "window_overlap"
            elif (self.settings.qa_pair_lone_question and len(candidates) == 1
                  and age <= LONE_QUESTION_MAX_AGE):
                # opt-in: a single, very recent open question with no keyword
                # overlap. OFF by default because it can create false pairs.
                score = 0.2
                method = "window_lone"
            else:
                continue
            if best is None or score > best[0]:
                best = (score, {**candidate, "_method": method})

        if best is None:
            logger.debug("window pairing: no relevant candidate among %s", len(candidates))
            return None

        candidate = best[1]
        method = candidate.pop("_method", "window_overlap")
        return {
            "method": "window",
            "confidence": PAIR_CONFIDENCE.get(method, 0.7),
            "question_text": candidate.get("question_text") or "",
            "question_message_id": int(candidate.get("telegram_message_id")),
            "question_timestamp": _parse_dt(candidate.get("message_timestamp")),
            "member_user_id": int(candidate.get("member_user_id")),
            "member_username": candidate.get("member_username"),
            "is_question": bool(candidate.get("is_question", True)),
            "pending_id": candidate.get("id"),
        }

    @staticmethod
    def _overlap(question_text: str, answer_keywords: set[str],
                 answer_crypto: set[str], answer_topic: str) -> int:
        question_keywords = set(extract_keywords(question_text, max_keywords=20))
        question_crypto = set(extract_crypto_terms(question_text))
        overlap = len(question_keywords & answer_keywords)
        overlap += len(question_crypto & answer_crypto)
        if answer_topic != "general" and classify_topic(question_text) == answer_topic:
            overlap += 1
        return overlap

    def _save_pair(self, *, admin_message: Any, admin_id: int, admin_username: str | None,
                   answer_text: str, answer_time: datetime,
                   pairing: dict[str, Any]) -> int | None:
        qa_id = self.db.save_qa_memory(
            chat_id=int(admin_message.chat.id),
            member_user_id=pairing["member_user_id"],
            member_username=pairing.get("member_username"),
            question_message_id=pairing["question_message_id"],
            question_text=pairing["question_text"],
            admin_message_id=int(admin_message.message_id),
            admin_id=admin_id,
            admin_answer_text=answer_text,
            question_timestamp=pairing.get("question_timestamp"),
            answer_timestamp=answer_time,
            pair_method=pairing["method"],
            pair_confidence=pairing["confidence"],
            is_question=bool(pairing.get("is_question", True)),
            embedding=self._embedding(f"{pairing['question_text']} {answer_text}"),
        )
        if not qa_id:
            return None
        chat_id = int(admin_message.chat.id)
        question_message_id = pairing["question_message_id"]

        # close the loop: pending question + analytics log + admin message flag
        if pairing.get("pending_id"):
            self.db.mark_pending_paired(int(pairing["pending_id"]), qa_id)
        else:
            self.db.mark_pending_paired_by_message(chat_id, question_message_id, qa_id)
        self.db.mark_question_answered(chat_id, question_message_id)
        self.db.mark_admin_message_as_answer(chat_id, int(admin_message.message_id))
        if admin_username:
            logger.debug("paired answer by @%s", admin_username)
        return qa_id


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def summarize_tracking(result: dict[str, Any]) -> str:
    """Compact one-line summary for logs."""
    try:
        return json.dumps({k: v for k, v in result.items() if k != "question_text"},
                          ensure_ascii=False, default=str)
    except Exception:  # pragma: no cover
        return str(result)

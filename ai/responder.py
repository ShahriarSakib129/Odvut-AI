"""Answer pipeline: retrieval -> prompt -> Groq -> safety post-processing.

This is the single entry point used by every handler (``/ask``, mentions,
replies, private chat). It is deliberately synchronous so the Telegram layer can
run it inside a worker thread (``asyncio.to_thread``) and never block the event
loop.

Cost control implemented here:

* answer cache (memory + PostgreSQL) short-circuits identical questions
* prompt size is bounded by ``MAX_CONTEXT_CHARS``
* a single Groq call per question (retries only on transient failures)
* token/latency usage is recorded in ``ai_usage_log`` for observability
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Sequence

from config import Settings
from utils.logger import get_logger, redact
from utils.text import detect_language, truncate

from .groq_client import AIResult, GroqClient
from .prompts import build_messages, estimate_prompt_chars, fallback_message
from .retrieval import MemoryContext, RetrievalEngine, summarize_sources

logger = get_logger(__name__)

HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
PREFIX_RE = re.compile(
    r"^\s*(উত্তর|answer|ai|assistant|bot)\s*[:：\-–]\s*", re.IGNORECASE
)
MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
IMPERSONATION_PATTERNS = (
    re.compile(r"আমি\s+(একজন\s+)?admin\b", re.IGNORECASE),
    re.compile(r"আমার\s+নাম\s+.{0,20}admin", re.IGNORECASE),
    re.compile(r"\bi\s+am\s+(the\s+)?admin\b", re.IGNORECASE),
    re.compile(r"\bi'?m\s+(the\s+)?admin\b", re.IGNORECASE),
    re.compile(r"\badmin\s+bolche[n]?\b.*\bami\b", re.IGNORECASE),
)


@dataclass
class AnswerResult:
    """Everything a handler needs to reply to Telegram."""

    text: str
    used_memory: bool = False
    cached: bool = False
    error_kind: str = "none"
    error: str = ""
    model: str = ""
    latency_ms: int = 0
    tokens: int = 0
    prompt_chars: int = 0
    context_stats: dict[str, Any] = field(default_factory=dict)
    sources: str = ""
    trace_id: str = ""
    fallback_key: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text) and self.error_kind in {"none", "extractive"}


def sanitize_answer(text: str, settings: Settings) -> tuple[str, list[str]]:
    """Clean model output and flag policy violations. Returns ``(text, issues)``."""
    issues: list[str] = []
    if not text:
        return "", ["empty"]
    clean = HEADING_RE.sub("", text)
    clean = PREFIX_RE.sub("", clean.lstrip())
    clean = MULTI_NEWLINE_RE.sub("\n\n", clean).strip()
    clean = clean.replace("```", "")

    for pattern in IMPERSONATION_PATTERNS:
        if pattern.search(clean):
            issues.append("impersonation")
            break

    if len(clean) > settings.max_answer_chars:
        clean = truncate(clean, settings.max_answer_chars, "…")
    return clean, issues


def extractive_fallback(context: MemoryContext, settings: Settings,
                        reason: str = "") -> str:
    """Answer without the LLM: quote the best stored memory.

    Used when the Groq API is unavailable but we do have relevant, verified
    admin memory -- far more useful than an error message.
    """
    if not context.has_memory:
        return ""
    lines: list[str] = []
    if reason == "rate_limited":
        lines.append("⏳ AI একটু ব্যস্ত, তাই Admin-এর আগের শেয়ার করা তথ্য থেকে দিচ্ছি:")
    else:
        lines.append("ℹ️ AI service এখন unavailable, তাই Admin-এর আগের তথ্য থেকে দিচ্ছি:")
    if context.qa_pairs:
        pair = context.qa_pairs[0]
        lines.append(
            f"\nপ্রশ্ন (আগের): {truncate(pair.question, 160)}\n"
            f"Admin-এর উত্তর: {truncate(pair.answer, 500)}"
        )
    elif context.admin_memories:
        item = context.admin_memories[0]
        date = item.timestamp.strftime("%d %b %Y") if item.timestamp else "?"
        lines.append(f"\nAdmin ({date}): {truncate(item.text, 600)}")
    lines.append("\n(সরাসরি নিশ্চিত উত্তর দিতে Admin-কে জিজ্ঞেস করো।)")
    return "\n".join(lines)


class AnswerService:
    """Orchestrates retrieval + generation and records observability data."""

    def __init__(self, settings: Settings, db: Any, retriever: RetrievalEngine,
                 groq: GroqClient, cache: Any = None):
        self.settings = settings
        self.db = db
        self.retriever = retriever
        self.groq = groq
        self.cache = cache
        self.counters = {"answers": 0, "cached": 0, "failed": 0, "extractive": 0}

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def answer(self, question: str, *, chat_id: int, user_id: int | None = None,
               username: str | None = None, use_history: bool = True,
               use_cache: bool = True, include_disclosure: bool = False,
               record_usage: bool = True) -> AnswerResult:
        started = time.time()
        trace_id = uuid.uuid4().hex[:12]
        settings = self.settings
        text = (question or "").strip()

        result = AnswerResult(text="", trace_id=trace_id)
        if not text:
            result.text = fallback_message("empty")
            result.error_kind = "empty_question"
            return result
        if len(text) > settings.max_question_chars:
            text = truncate(text, settings.max_question_chars)
            logger.debug("question truncated to %s chars", settings.max_question_chars)

        # 1. cache ---------------------------------------------------------
        cached_hit = None
        if use_cache and self.cache is not None:
            cached_hit = self.cache.get(chat_id, text)
        if cached_hit is not None:
            reply = self._decorate(cached_hit.text, include_disclosure)
            self.counters["cached"] += 1
            logger.info("answer served from cache (trace=%s source=%s)", trace_id,
                        cached_hit.source)
            if record_usage:
                self.db.log_ai_usage(
                    chat_id=chat_id, user_id=user_id, model=cached_hit.model,
                    status="ok", cached=True, used_memory=cached_hit.used_memory,
                    latency_ms=int((time.time() - started) * 1000),
                    question_hash=_question_hash(text),
                )
            return AnswerResult(
                text=reply, used_memory=cached_hit.used_memory, cached=True,
                model=cached_hit.model, trace_id=trace_id,
                latency_ms=int((time.time() - started) * 1000),
            )

        # 2. retrieval -----------------------------------------------------
        try:
            context = self.retriever.retrieve(chat_id=chat_id, question=text)
        except Exception as exc:
            logger.error("retrieval failed: %s", redact(str(exc))[:200])
            result.text = fallback_message("db_error")
            result.error_kind = "retrieval_error"
            result.error = redact(str(exc))[:200]
            self.counters["failed"] += 1
            if record_usage:
                self.db.log_ai_usage(chat_id=chat_id, user_id=user_id, status="error",
                                     error_type="retrieval_error")
            return result

        result.context_stats = context.stats()
        result.used_memory = context.has_memory
        result.sources = summarize_sources(context)

        # 3. AI disabled? ---------------------------------------------------
        if not self.groq.enabled:
            logger.warning("AI unavailable (trace=%s): GROQ_ENABLED=%s key_set=%s",
                           trace_id, settings.groq_enabled, bool(settings.groq_api_key))
            extracted = extractive_fallback(context, settings) if context.has_memory else ""
            if settings.ai_answer_without_memory and not extracted:
                extracted = fallback_message("no_ai")
            result.text = self._decorate(extracted or fallback_message("no_ai"),
                                         include_disclosure)
            result.error_kind = "no_ai"
            result.error = "GROQ_API_KEY missing or GROQ_ENABLED=false"
            self.counters["failed"] += 1
            if record_usage:
                self.db.log_ai_usage(chat_id=chat_id, user_id=user_id, model=settings.groq_model,
                                     status="error", error_type="no_ai",
                                     used_memory=context.has_memory,
                                     memory_count=len(context.admin_memories),
                                     qa_count=len(context.qa_pairs))
            return result

        # 4. prompt + call --------------------------------------------------
        history = self._load_history(chat_id, user_id) if use_history and user_id else []
        messages = build_messages(settings, context, text, history=history)
        prompt_chars = estimate_prompt_chars(messages)
        result.prompt_chars = prompt_chars
        logger.info("groq request (trace=%s model=%s prompt_chars=%s memory=%s qa=%s)",
                    trace_id, settings.groq_model, prompt_chars,
                    len(context.admin_memories), len(context.qa_pairs))

        try:
            ai: AIResult = self.groq.chat(messages)
        except Exception as exc:  # a broken client must never take the bot down
            logger.error("groq client raised (trace=%s): %s", trace_id, redact(str(exc))[:200])
            ai = AIResult(ok=False, error=f"client error: {exc}", error_kind="api_error")

        result.latency_ms = ai.latency_ms
        result.tokens = ai.total_tokens
        result.model = ai.model or settings.groq_model

        if ai.failed:
            self.counters["failed"] += 1
            result.error_kind = ai.error_kind or "api_error"
            result.error = ai.short_error(200)
            logger.warning("groq failed (trace=%s kind=%s): %s", trace_id, result.error_kind,
                           result.error)
            extracted = extractive_fallback(context, settings, reason=ai.error_kind)
            if extracted and ai.error_kind in {"rate_limited", "timeout", "network", "api_error"}:
                result.text = self._decorate(extracted, include_disclosure)
                result.error_kind = "extractive"
                self.counters["extractive"] += 1
            else:
                result.text = fallback_message(ai.error_kind or "generic")
            if record_usage:
                self.db.log_ai_usage(
                    chat_id=chat_id, user_id=user_id, model=settings.groq_model,
                    status="error", error_type=result.error_kind, used_memory=context.has_memory,
                    latency_ms=ai.latency_ms, memory_count=len(context.admin_memories),
                    qa_count=len(context.qa_pairs), question_hash=_question_hash(text),
                )
            return result

        # 5. post-process ---------------------------------------------------
        clean, issues = sanitize_answer(ai.text, settings)
        if issues:
            logger.warning("answer policy issue(s) %s (trace=%s) -- disclosure added",
                           issues, trace_id)
            include_disclosure = True
        result.text = self._decorate(clean, include_disclosure)
        result.error_kind = "none"
        self.counters["answers"] += 1

        # 6. cache + bookkeeping --------------------------------------------
        if self.cache is not None:
            self.cache.set(chat_id, text, clean, used_memory=context.has_memory,
                           model=result.model)
        if context.qa_pairs:
            try:
                self.db.record_memory_usage([p.id for p in context.qa_pairs])
            except Exception:
                pass
        self._save_history(chat_id, user_id, text, clean)
        if record_usage:
            self.db.log_ai_usage(
                chat_id=chat_id, user_id=user_id, model=result.model, status="ok",
                used_memory=context.has_memory, prompt_tokens=ai.prompt_tokens,
                completion_tokens=ai.completion_tokens, total_tokens=ai.total_tokens,
                latency_ms=ai.latency_ms, memory_count=len(context.admin_memories),
                qa_count=len(context.qa_pairs), question_hash=_question_hash(text),
            )
        logger.info("answer ready (trace=%s latency=%sms tokens=%s memory=%s)",
                    trace_id, ai.latency_ms, ai.total_tokens, context.item_count)
        return result

    def answer_with_context(self, question: str, *, chat_id: int,
                            user_id: int | None = None) -> tuple[AnswerResult, MemoryContext]:
        """Same as :meth:`answer` but also returns the retrieved context (used by ``/memory``)."""
        result = self.answer(question, chat_id=chat_id, user_id=user_id)
        context = self.retriever.retrieve(chat_id=chat_id, question=question)
        return result, context

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _decorate(self, text: str, include_disclosure: bool) -> str:
        text = (text or "").strip()
        if not text:
            return fallback_message("generic")
        if include_disclosure and self.settings.ai_disclosure_in_group:
            footer = self.settings.ai_disclosure_text.strip()
            if footer:
                candidate = f"{text}\n\n{footer}"
                if len(candidate) <= self.settings.max_answer_chars:
                    return candidate
        return text

    def _load_history(self, chat_id: int, user_id: int | None) -> list[dict[str, str]]:
        if not user_id:
            return []
        try:
            session = self.db.get_session(chat_id, int(user_id))
        except Exception:
            return []
        if not session:
            return []
        history = session.get("history") or []
        if isinstance(history, str):
            return []
        return [
            {"role": str(turn.get("role")), "content": str(turn.get("content"))}
            for turn in history[-4:]
            if isinstance(turn, dict) and turn.get("content")
        ]

    def _save_history(self, chat_id: int, user_id: int | None, question: str,
                      answer_text: str) -> None:
        if not user_id:
            return
        try:
            existing = self.db.get_session(chat_id, int(user_id)) or {}
            history = list(existing.get("history") or [])
        except Exception:
            history = []
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer_text})
        try:
            self.db.save_session(chat_id, int(user_id), history[-8:], ttl_seconds=1800,
                                 last_question=question, last_answer=answer_text)
        except Exception as exc:
            logger.debug("session save failed: %s", exc)

    def health(self) -> dict[str, Any]:
        return {
            "enabled": self.groq.enabled,
            "model": self.settings.groq_model,
            "counters": dict(self.counters),
            "cache": self.cache.stats() if self.cache is not None else None,
        }


def _question_hash(question: str) -> str:
    from utils.text import sha256_short
    return sha256_short(question.strip().lower(), 24)


def answer_preview(result: AnswerResult, limit: int = 600) -> str:
    return truncate((result.text or "").replace("\n", " "), limit)


def language_note(question: str) -> str:
    return detect_language(question)


def format_sources_for_admin(result: AnswerResult) -> str:
    if not result.sources or result.sources == "—":
        return "কোনো memory ব্যবহার হয়নি (AI general knowledge)"
    return result.sources

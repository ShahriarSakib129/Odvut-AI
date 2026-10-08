"""Retrieval engine: turns a member question into a compact memory context.

Pipeline (requirement #8 and #17)::

    question
      -> keyword / crypto-term extraction
      -> candidate fetch (PostgreSQL full-text search + trigram + recency)
      -> relevance scoring (lexical + coverage + semantic + recency + topic)
      -> de-duplication
      -> ranking & limits (MAX_MEMORY_RESULTS / MAX_QA_RESULTS)
      -> context rendering inside MAX_CONTEXT_CHARS

Nothing in this module talks to Telegram, and only reads from the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from config import Settings
from utils.logger import get_logger
from utils.text import (
    classify_topic,
    extract_crypto_terms,
    extract_keywords,
    jaccard,
    text_similarity,
    tokenize,
    truncate,
    truncate_middle,
)

from .embeddings import cosine_similarity, embed_text

logger = get_logger(__name__)

# scoring weights (sum = 1.0 for the non-boost part)
W_LEXICAL = 0.34
W_COVERAGE = 0.26
W_SEMANTIC = 0.24
W_RECENCY = 0.16
TOPIC_BONUS = 0.08
CRYPTO_BONUS = 0.06
ANSWER_STYLE_BONUS = 0.04
#: minimum lexical+coverage+semantic evidence for a memory to be used
MIN_SIGNAL = 0.02
#: hash-embeddings have a small baseline similarity for unrelated short texts;
#: subtract it so a random overlap cannot masquerade as "semantic relevance"
SEMANTIC_FLOOR = 0.12


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _age_days(when: datetime | None, now: datetime | None = None) -> float:
    if when is None:
        return 3650.0
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - when).total_seconds() / 86400.0)


def recency_weight(when: datetime | None, half_life_days: float,
                   now: datetime | None = None) -> float:
    """Exponential decay in ``(0, 1]`` -- newer memories score higher."""
    if not half_life_days or half_life_days <= 0:
        return 1.0
    return float(0.5 ** (_age_days(when, now) / half_life_days))


# --------------------------------------------------------------------------- #
# data containers
# --------------------------------------------------------------------------- #
@dataclass
class MemoryItem:
    """A single admin message used as memory."""

    id: int
    text: str
    timestamp: datetime | None
    message_id: int | None = None
    topic: str = "general"
    keywords: list[str] = field(default_factory=list)
    crypto_terms: list[str] = field(default_factory=list)
    score: float = 0.0
    parts: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "message_id": self.message_id,
            "topic": self.topic,
            "score": round(self.score, 4),
        }


@dataclass
class QAPair:
    """A verified member-question / admin-answer pair."""

    id: int
    question: str
    answer: str
    answer_timestamp: datetime | None
    question_timestamp: datetime | None = None
    question_message_id: int | None = None
    admin_message_id: int | None = None
    member_username: str | None = None
    topic: str = "general"
    keywords: list[str] = field(default_factory=list)
    crypto_terms: list[str] = field(default_factory=list)
    pair_method: str = "reply"
    score: float = 0.0
    parts: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "answer": self.answer,
            "timestamp": self.answer_timestamp.isoformat() if self.answer_timestamp else None,
            "topic": self.topic,
            "pair_method": self.pair_method,
            "score": round(self.score, 4),
        }


@dataclass
class MemoryContext:
    """Everything the prompt builder needs, already trimmed to size."""

    admin_memories: list[MemoryItem] = field(default_factory=list)
    qa_pairs: list[QAPair] = field(default_factory=list)
    rendered: str = ""
    context_chars: int = 0
    candidates_considered: int = 0
    admin_scanned: int = 0
    qa_scanned: int = 0
    dropped_duplicates: int = 0
    dropped_irrelevant: int = 0
    truncated: bool = False
    recent_fallback: bool = False

    @property
    def has_memory(self) -> bool:
        return bool(self.admin_memories or self.qa_pairs)

    @property
    def item_count(self) -> int:
        return len(self.admin_memories) + len(self.qa_pairs)

    def stats(self) -> dict[str, Any]:
        return {
            "admin_memories": len(self.admin_memories),
            "qa_pairs": len(self.qa_pairs),
            "candidates": self.candidates_considered,
            "admin_scanned": self.admin_scanned,
            "qa_scanned": self.qa_scanned,
            "duplicates_dropped": self.dropped_duplicates,
            "irrelevant_dropped": self.dropped_irrelevant,
            "context_chars": self.context_chars,
            "truncated": self.truncated,
            "recent_fallback": self.recent_fallback,
        }


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
class RetrievalEngine:
    """Scores and selects admin memories / Q&A pairs relevant to a question."""

    def __init__(self, db: Any, settings: Settings):
        self.db = db
        self.settings = settings

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def retrieve(self, *, chat_id: int, question: str,
                 question_ts: datetime | None = None) -> MemoryContext:
        """Build a memory context for ``question``."""
        settings = self.settings
        clean_question = (question or "").strip()
        context = MemoryContext()
        if not clean_question:
            return context

        keywords = extract_keywords(clean_question, max_keywords=12)
        crypto_terms = extract_crypto_terms(clean_question)
        question_topic = classify_topic(clean_question)
        question_vector = (
            embed_text(clean_question, settings.embedding_dim) if settings.embedding_enabled else []
        )

        admin_rows: list[dict[str, Any]] = []
        qa_rows: list[dict[str, Any]] = []

        if settings.max_memory_results > 0:
            try:
                admin_rows = self.db.search_admin_messages(
                    chat_id=chat_id,
                    query_text=clean_question,
                    keywords=keywords,
                    limit=settings.candidate_limit,
                ) or []
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("admin memory search failed: %s", exc)
        if settings.max_qa_results > 0:
            try:
                qa_rows = self.db.search_qa_memory(
                    chat_id=chat_id,
                    query_text=clean_question,
                    keywords=keywords,
                    limit=settings.candidate_limit,
                ) or []
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("qa memory search failed: %s", exc)

        context.admin_scanned = len(admin_rows)
        context.qa_scanned = len(qa_rows)
        context.candidates_considered = len(admin_rows) + len(qa_rows)

        scored_admin = self._score_admin_rows(
            admin_rows, clean_question, keywords, crypto_terms, question_topic, question_vector
        )
        scored_qa = self._score_qa_rows(
            qa_rows, clean_question, keywords, crypto_terms, question_topic, question_vector
        )

        selected_admin, dropped_admin = self._select(
            scored_admin, settings.max_memory_results
        )
        selected_qa, dropped_qa = self._select(scored_qa, settings.max_qa_results)

        if not selected_admin and not selected_qa:
            # Cold start / question that matches nothing: fall back to the newest
            # items so the assistant still knows the admin's latest stance. The
            # prompt marks this context as "recent chatter", not as an answer.
            fb_admin, fb_qa = self._fallback_recent(
                chat_id, clean_question, keywords, crypto_terms, question_topic, question_vector
            )
            if fb_admin or fb_qa:
                context.recent_fallback = True
                selected_admin, dropped_admin = self._select(
                    fb_admin, settings.max_memory_results,
                    require_signal=False, min_score=0.0,
                )
                selected_qa, dropped_qa = self._select(
                    fb_qa, settings.max_qa_results,
                    require_signal=False, min_score=0.0,
                )
                logger.info("retrieval: used recent-memory fallback (%s admin + %s qa)",
                            len(selected_admin), len(selected_qa))

        # interleave de-duplication across both lists (Q&A first: they are verified)
        selected_admin, dupes = self._dedupe_across(selected_qa, selected_admin)

        context.admin_memories = selected_admin
        context.qa_pairs = selected_qa
        context.dropped_duplicates = dropped_admin + dropped_qa + dupes
        context.dropped_irrelevant = dropped_admin + dropped_qa
        context.rendered, context.context_chars, context.truncated = self._render(
            selected_qa, selected_admin
        )
        if context.recent_fallback and context.rendered:
            context.rendered += ("\n\n[দ্রষ্টব্য] এই স্মৃতিগুলো প্রশ্নের সাথে সরাসরি মেলেনি; "
                                 "এগুলো সাম্প্রতিক কথাবার্তা। নিশ্চিত তথ্য না থাকলে তা স্পষ্ট বলবে।")
            context.context_chars = len(context.rendered)
        logger.info(
            "retrieval: selected %s admin memories + %s Q&A pairs (%s chars, %s candidates)",
            len(selected_admin), len(selected_qa), context.context_chars,
            context.candidates_considered,
        )
        return context

    # ------------------------------------------------------------------ #
    # scoring
    # ------------------------------------------------------------------ #
    def _score_admin_rows(self, rows: Sequence[dict[str, Any]], question: str,
                          keywords: Sequence[str], crypto_terms: Sequence[str],
                          question_topic: str,
                          question_vector: Sequence[float]) -> list[MemoryItem]:
        settings = self.settings
        items: list[MemoryItem] = []
        for row in rows:
            text = row.get("message_text") or ""
            if not text.strip():
                continue
            ts = _parse_dt(row.get("message_timestamp") or row.get("created_at"))
            item_keywords = list(row.get("keywords") or [])
            item_crypto = list(row.get("crypto_terms") or [])
            parts = self._score_parts(
                question=question, keywords=keywords, crypto_terms=crypto_terms,
                question_topic=question_topic, question_vector=question_vector,
                text=text, item_keywords=item_keywords, item_crypto=item_crypto,
                topic=str(row.get("topic") or "general"), timestamp=ts,
                stored_embedding=row.get("embedding"),
                fts_rank=float(row.get("fts_rank") or 0.0),
            )
            score = self._combine(parts)
            if row.get("is_answer"):
                score += ANSWER_STYLE_BONUS / 4.0
            items.append(MemoryItem(
                id=int(row.get("id") or 0),
                text=text,
                timestamp=ts,
                message_id=row.get("telegram_message_id"),
                topic=str(row.get("topic") or "general"),
                keywords=item_keywords,
                crypto_terms=item_crypto,
                score=score,
                parts=parts,
            ))
        return items

    def _score_qa_rows(self, rows: Sequence[dict[str, Any]], question: str,
                       keywords: Sequence[str], crypto_terms: Sequence[str],
                       question_topic: str,
                       question_vector: Sequence[float]) -> list[QAPair]:
        settings = self.settings
        pairs: list[QAPair] = []
        for row in rows:
            q_text = row.get("question_text") or ""
            a_text = row.get("admin_answer_text") or ""
            if not q_text.strip() and not a_text.strip():
                continue
            ts = _parse_dt(row.get("answer_timestamp") or row.get("created_at"))
            combined_for_scoring = f"{q_text}\n{a_text}"
            parts = self._score_parts(
                question=question, keywords=keywords, crypto_terms=crypto_terms,
                question_topic=question_topic, question_vector=question_vector,
                text=combined_for_scoring, item_keywords=list(row.get("keywords") or []),
                item_crypto=list(row.get("crypto_terms") or []),
                topic=str(row.get("topic") or "general"), timestamp=ts,
                stored_embedding=row.get("embedding"),
                fts_rank=float(row.get("fts_rank") or 0.0),
                extra_texts=(q_text,),
            )
            score = self._combine(parts) * settings.qa_priority_boost
            # verified answers are more trustworthy than raw chatter
            score += float(row.get("pair_confidence") or 0.9) * 0.03
            pairs.append(QAPair(
                id=int(row.get("id") or 0),
                question=q_text,
                answer=a_text,
                answer_timestamp=ts,
                question_timestamp=_parse_dt(row.get("question_timestamp")),
                question_message_id=row.get("question_message_id"),
                admin_message_id=row.get("admin_message_id"),
                member_username=row.get("member_username"),
                topic=str(row.get("topic") or "general"),
                keywords=list(row.get("keywords") or []),
                crypto_terms=list(row.get("crypto_terms") or []),
                pair_method=str(row.get("pair_method") or "reply"),
                score=score,
                parts=parts,
            ))
        return pairs

    def _score_parts(self, *, question: str, keywords: Sequence[str],
                     crypto_terms: Sequence[str], question_topic: str,
                     question_vector: Sequence[float], text: str,
                     item_keywords: Sequence[str], item_crypto: Sequence[str],
                     topic: str, timestamp: datetime | None,
                     stored_embedding: Any, fts_rank: float,
                     extra_texts: Sequence[str] = ()) -> dict[str, float]:
        settings = self.settings
        lexical = max(
            jaccard(keywords, item_keywords) if item_keywords else 0.0,
            jaccard(keywords, text),
        )
        if keywords and text:
            # token-boundary matching (a substring hit inside another word, e.g.
            # "er" inside "Leverage", must never count as a keyword match)
            text_tokens = set(tokenize(text))
            hits = sum(1 for kw in keywords if kw in text_tokens)
            lexical = max(lexical, min(1.0, hits / max(1, len(keywords))))
            # morphology tolerance: only for longer keywords ("নেওয়ার" ~ "নেওয়া")
            long_hits = sum(1 for kw in keywords if len(kw) >= 6 and kw in text)
            if long_hits:
                lexical = max(lexical, min(0.9, 0.6 * long_hits / max(1, len(keywords))))

        coverage = 0.0
        for candidate_text in (text, *extra_texts):
            if not candidate_text:
                continue
            tokens = set(tokenize(candidate_text))
            hits = sum(1 for kw in keywords if kw in tokens)
            coverage = max(coverage, hits / max(1, len(keywords)))

        semantic = 0.0
        if settings.embedding_enabled and question_vector:
            vector = _coerce_vector(stored_embedding)
            if not vector:
                vector = embed_text(text, settings.embedding_dim)
            raw = max(0.0, cosine_similarity(question_vector, vector))
            semantic = max(0.0, (raw - SEMANTIC_FLOOR) / (1.0 - SEMANTIC_FLOOR))

        fts = min(1.0, fts_rank) if fts_rank else 0.0
        lexical = max(lexical, fts * 0.9)

        recency = recency_weight(timestamp, settings.recency_half_life_days)
        topic_bonus = TOPIC_BONUS if (topic and topic == question_topic and topic != "general") else 0.0
        crypto_hits = len(set(crypto_terms) & set(item_crypto or _terms_in(text)))
        crypto_bonus = CRYPTO_BONUS if crypto_hits else 0.0

        return {
            "lexical": round(lexical, 4),
            "coverage": round(coverage, 4),
            "semantic": round(semantic, 4),
            "recency": round(recency, 4),
            "topic_bonus": topic_bonus,
            "crypto_bonus": crypto_bonus,
        }

    @staticmethod
    def _combine(parts: dict[str, float]) -> float:
        base = (
            W_LEXICAL * parts.get("lexical", 0.0)
            + W_COVERAGE * parts.get("coverage", 0.0)
            + W_SEMANTIC * parts.get("semantic", 0.0)
            + W_RECENCY * parts.get("recency", 0.0)
        )
        return base + parts.get("topic_bonus", 0.0) + parts.get("crypto_bonus", 0.0)

    # ------------------------------------------------------------------ #
    # selection helpers
    # ------------------------------------------------------------------ #
    def _select(self, items: Sequence[Any], limit: int, *,
                require_signal: bool = True,
                min_score: float | None = None) -> tuple[list[Any], int]:
        """Sort, drop low-relevance and near-duplicate items; apply a hard limit.

        ``require_signal`` also removes items that only scored on recency: a
        memory that shares *nothing* with the question must not pollute the
        prompt (recency alone would otherwise put the newest chatter in front of
        the model). The cold-start fallback path calls this with ``False``.
        """
        settings = self.settings
        floor = settings.min_relevance_score if min_score is None else min_score
        ranked = sorted(items, key=lambda item: item.score, reverse=True)
        kept: list[Any] = []
        dropped = 0
        for item in ranked:
            if len(kept) >= limit:
                dropped += 1
                continue
            if item.score < floor:
                dropped += 1
                continue
            if require_signal:
                parts = getattr(item, "parts", {}) or {}
                signal = (parts.get("lexical", 0.0) + parts.get("coverage", 0.0)
                          + parts.get("semantic", 0.0))
                if signal < MIN_SIGNAL:
                    dropped += 1
                    continue
            candidate_text = _item_text(item)
            if any(
                text_similarity(candidate_text, _item_text(existing)) >= settings.dedupe_threshold
                for existing in kept
            ):
                dropped += 1
                continue
            kept.append(item)
        return kept, dropped

    @staticmethod
    def _dedupe_across(qa_pairs: Sequence[QAPair], admin_items: list[MemoryItem]
                       ) -> tuple[list[MemoryItem], int]:
        """An admin message that is already inside a Q&A answer is redundant."""
        removed = 0
        kept: list[MemoryItem] = []
        for item in admin_items:
            redundant = False
            for pair in qa_pairs:
                if item.message_id and pair.admin_message_id == item.message_id:
                    redundant = True
                    break
                if pair.answer and text_similarity(item.text, pair.answer) >= 0.93:
                    redundant = True
                    break
            if redundant:
                removed += 1
            else:
                kept.append(item)
        return kept, removed

    def _fallback_recent(self, chat_id: int, question: str, keywords: Sequence[str],
                         crypto_terms: Sequence[str], question_topic: str,
                         question_vector: Sequence[float]) -> tuple[list[MemoryItem], list[QAPair]]:
        settings = self.settings
        if settings.include_recent_fallback <= 0:
            return [], []
        rows: list[dict[str, Any]] = []
        try:
            rows = self.db.get_recent_memories(chat_id, limit=settings.include_recent_fallback) or []
        except Exception as exc:  # pragma: no cover
            logger.warning("recent-memory fallback failed: %s", exc)
            return [], []
        admin_rows = [r for r in rows if r.get("kind") == "admin"]
        qa_rows = [r for r in rows if r.get("kind") == "qa"]
        admin_items = self._score_admin_rows(admin_rows, question, keywords, crypto_terms,
                                            question_topic, question_vector)
        qa_pairs = self._score_qa_rows(qa_rows, question, keywords, crypto_terms,
                                       question_topic, question_vector)
        # fallback items are recent by construction: recency is the signal here,
        # so the relevance floor / signal requirement is relaxed on purpose.
        admin_items = [item for item in admin_items if item.text][
            : max(1, settings.max_memory_results)
        ]
        qa_pairs = [pair for pair in qa_pairs if pair.question or pair.answer][
            : max(1, settings.max_qa_results)
        ]
        return admin_items, qa_pairs

    # ------------------------------------------------------------------ #
    # rendering
    # ------------------------------------------------------------------ #
    def _render(self, qa_pairs: Sequence[QAPair], admin_items: Sequence[MemoryItem]
                ) -> tuple[str, int, bool]:
        settings = self.settings
        budget = settings.max_context_chars
        blocks: list[str] = []
        used = 0
        truncated = False

        if qa_pairs:
            header = "### নিশ্চিত প্রশ্ন-উত্তর (Member প্রশ্ন + Admin উত্তর)\n"
            block_lines = [header]
            used += len(header)
            for pair in qa_pairs:
                date = pair.answer_timestamp.strftime("%Y-%m-%d") if pair.answer_timestamp else "?"
                topic = f" [{pair.topic}]" if pair.topic and pair.topic != "general" else ""
                q = truncate(pair.question.replace("\n", " "), 300)
                a = truncate_middle(pair.answer.replace("\n", " "), 900)
                line = f"- ({date}{topic}) প্রশ্ন: {q}\n  Admin উত্তর: {a}\n"
                if used + len(line) > budget:
                    truncated = True
                    break
                block_lines.append(line)
                used += len(line)
            if len(block_lines) > 1:
                blocks.append("".join(block_lines))

        if admin_items:
            header = "\n### Admin-এর নিজের বক্তব্য / বিশ্লেষণ (time-stamped)\n"
            block_lines = [header]
            used += len(header)
            for item in admin_items:
                date = item.timestamp.strftime("%Y-%m-%d") if item.timestamp else "?"
                topic = f" [{item.topic}]" if item.topic and item.topic != "general" else ""
                text = truncate_middle(item.text.replace("\n", " "), 700)
                line = f"- ({date}{topic}) {text}\n"
                if used + len(line) > budget:
                    truncated = True
                    break
                block_lines.append(line)
                used += len(line)
            if len(block_lines) > 1:
                blocks.append("".join(block_lines))

        rendered = "".join(blocks).strip()
        if truncated:
            note = "\n\n(নোট: কনটেক্সট সীমিত রাখার জন্য আরও কিছু পুরোনো memory বাদ দেওয়া হয়েছে।)"
            rendered += note
            used += len(note)
        return rendered, used, truncated


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _item_text(item: Any) -> str:
    """Text of a :class:`MemoryItem` or a :class:`QAPair` (for dedupe/limits)."""
    for attribute in ("text", "answer", "question"):
        value = getattr(item, attribute, None)
        if value:
            return str(value)
    return ""


def _coerce_vector(value: Any) -> list[float]:
    """Convert a database ``real[]`` value into ``list[float]``."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        try:
            return [float(v) for v in value]
        except (TypeError, ValueError):
            return []
    if isinstance(value, str):
        cleaned = value.strip().strip("{}[]")
        if not cleaned:
            return []
        out: list[float] = []
        for chunk in cleaned.split(","):
            try:
                out.append(float(chunk))
            except ValueError:
                return []
        return out
    return []


def _terms_in(text: str) -> list[str]:
    return extract_crypto_terms(text)


def build_context_preview(context: MemoryContext, max_chars: int = 400) -> str:
    """Short, log-safe preview of what was retrieved."""
    return truncate((context.rendered or "").replace("\n", " "), max_chars)


def relevance_table(context: MemoryContext, limit: int = 5) -> list[dict[str, Any]]:
    """Debug helper: the highest scoring items and their score components."""
    rows: list[dict[str, Any]] = []
    for pair in context.qa_pairs[:limit]:
        rows.append({"kind": "qa", "score": round(pair.score, 4), **pair.parts,
                     "preview": truncate(pair.question, 60)})
    for item in context.admin_memories[:limit]:
        rows.append({"kind": "admin", "score": round(item.score, 4), **item.parts,
                     "preview": truncate(item.text, 60)})
    rows.sort(key=lambda row: row["score"], reverse=True)
    return rows[:limit]


def summarize_sources(context: MemoryContext, limit: int = 3) -> str:
    """Human readable list of the sources used, for admin inspection."""
    lines: list[str] = []
    for pair in context.qa_pairs[:limit]:
        date = pair.answer_timestamp.strftime("%d %b %Y") if pair.answer_timestamp else "?"
        lines.append(f"• Q&A ({date}, score {pair.score:.2f})")
    for item in context.admin_memories[:limit]:
        date = item.timestamp.strftime("%d %b %Y") if item.timestamp else "?"
        lines.append(f"• admin message ({date}, score {item.score:.2f})")
    return "\n".join(lines) if lines else "—"


def merge_memory_texts(*collections: Iterable[Any]) -> list[str]:
    texts: list[str] = []
    for collection in collections:
        for item in collection or []:
            text = getattr(item, "text", None) or getattr(item, "answer", None)
            if text:
                texts.append(text)
    return texts

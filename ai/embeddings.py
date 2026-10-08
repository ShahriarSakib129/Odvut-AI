"""Dependency-free semantic embeddings.

Why not a neural model?

* Render's free tier has 512 MB RAM and a 0.1 CPU: downloading/loading a
  sentence-transformer model is not realistic.
* A hosted embedding API would add another paid dependency, which the project
  must avoid (requirement #17).

Instead we use a **hashing vectoriser over a hand-built multilingual
vocabulary** (crypto terms, Bangla stop-words, English stop-words) combined
with sub-word character n-grams. That gives:

* semantic-ish behaviour for this domain (``"entry"`` and ``"buy now"``,
  ``"bikroy"``/``"sell"`` etc. share dimensions),
* Bangla script support (character n-grams work on any script),
* deterministic, fast, offline and free computation.

The vectors are stored in the ``embedding real[]`` columns, so a real neural
model can be dropped in later by replacing :func:`embed_text` only -- no schema
change required.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable, Sequence

from utils.text import (
    BENGALI_RANGE,
    CRYPTO_ALIASES,
    CRYPTO_TERMS,
    STOPWORDS,
    detect_language,
    normalize_text,
    sanitize_incoming_text,
    tokenize,
)

DEFAULT_DIM = 512

# --------------------------------------------------------------------------- #
# semantic groups: tokens inside a group share a "concept" dimension
# --------------------------------------------------------------------------- #
CONCEPT_GROUPS: dict[str, tuple[str, ...]] = {
    # Bangla + Banglish synonyms are grouped with their English equivalents so a
    # Bengali question can match an English/Banglish memory (and vice versa).
    "buy": ("buy", "entry", "long", "kine", "kinbo", "nibo", "kena", "কিনব", "ক্রয়",
            "accumulate", "dca", "bid", "bullish",
            "এন্ট্রি", "এনট্রি", "কেনা", "কিনবো", "নেবো", "নেওয়া", "বাই", "কেনাকাটা",
            "neowa", "nebo", "kenakata", "buying"),
    "sell": ("sell", "exit", "short", "beche", "bikroy", "বিক্রয়", "book", "profit",
             "take", "closed", "bearish",
             "বিক্রি", "বেচব", "বেচে", "বিক্রয়ের", "beche", "sold", "selling"),
    "hold": ("hold", "wait", "waiting", "opekkha", "ধর", "patience", "confirm",
             "confirmation", "dekhi", "রাখো", "রাখব", "অপেক্ষা", "অপেক্ষা",
             "opekkha", "dhairya", "ধৈর্য", "dhairjo", "waiting", "rakho"),
    "support": ("support", "zone", "level", "demand", "floor", "সাপোর্ট", "লেভেল",
                "লেভেলের", "সাপোর্টে"),
    "resistance": ("resistance", "supply", "ceiling", "রেজিস্ট্যান্স", "রেজিস্টেন্স"),
    "risk": ("risk", "risky", "safe", "stop", "sl", "stoploss", "loss", "leverage",
             "margin", "liquid", "liquidation", "protection",
             "ঝুঁকি", "ঝুকি", "রিস্ক", "লস", "স্টপ", "লিভারেজ", "ক্ষতি"),
    "target": ("target", "tp", "takeprofit", "goal", "উপরে", "profit", "moon",
               "টার্গেট", "প্রফিট", "লাভ", "মুনাফা"),
    "price_up": ("up", "pump", "bull", "bullish", "upward", "rise", "breakout", "cross",
                 "above", "upore", "বাড়", "উপরে", "বাড়বে", "উঠবে", "উঠবে",
                 "uthbe", "barbe", "upore jabe"),
    "price_down": ("down", "dump", "bear", "bearish", "downward", "fall", "breakdown",
                   "below", "niche", "নিচে", "পতন", "নামবে", "পড়বে", "ডাম্প",
                   "nambe", "porbe", "niche jabe"),
    "time": ("now", "ekhon", "today", "ajke", "kal", "tomorrow", "wait", "later",
             "timeframe", "1h", "4h", "1d", "weekly", "daily", "আজ", "এখন", "এখনি",
             "আজকে", "কাল", "পরে", "সময়", "ধৈর্য", "hold koro", "opekkha koro"),
    "question": ("ki", "kemon", "keno", "kothay", "kobe", "koto", "how", "what", "when",
                 "why", "where", "কি", "কেমন", "কেন", "কোথায়", "কবে", "কত", "কিভাবে"),
    "opinion": ("mone", "hoy", "think", "view", "opinion", "montobbo", "মত", "মনে",
                "মতামত", "পরামর্শ", "মনে হয়"),
    "money": ("usdt", "usd", "taka", "inr", "capital", "balance", "fund", "money",
              "invest", "পুঁজি", "টাকা", "বিনিয়োগ"),
    "news": ("news", "fed", "cpi", "etf", "macro", "announcement", "খবর", "সংবাদ"),
    # NOTE: individual tickers are deliberately NOT grouped together -- otherwise
    # "BTC kemon?" would look similar to "ETH kemon?". Tickers are matched by the
    # token dimension (and by CRYPTO_ALIASES, so "bitcoin" ~ "btc").
    "crypto_generic": ("crypto", "cryptocurrency", "coin", "coins", "token", "tokens",
                       "altcoin", "altcoins", "alt", "কয়েন", "ক্রিপ্টো", "টোকেন",
                       "মার্কেট", "market", "মার্কেটের", "বাজার"),
}

#: number of hash buckets reserved for the concept space
_CONCEPT_BUCKETS = 64
#: number of hash buckets reserved for token space
_TOKEN_BUCKETS = 224
#: number of hash buckets reserved for character n-gram space
_GRAM_BUCKETS = DEFAULT_DIM - _CONCEPT_BUCKETS - _TOKEN_BUCKETS  # 224

_CONCEPT_INDEX: dict[str, str] = {}
for _concept, _words in CONCEPT_GROUPS.items():
    for _word in _words:
        _CONCEPT_INDEX.setdefault(_word.lower(), _concept)

_URL_RE = re.compile(r"https?://\S+")


def _hash_index(value: str, buckets: int, salt: str = "") -> int:
    digest = hashlib.blake2b((salt + value).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % buckets


def _hash_sign(value: str, salt: str = "") -> float:
    digest = hashlib.blake2b((salt + value).encode("utf-8"), digest_size=1).digest()
    return 1.0 if digest[0] & 1 else -1.0


def _char_ngrams(text: str, n: int = 3) -> list[str]:
    compact = text.replace(" ", "_")
    if not compact:
        return []
    if len(compact) < n:
        return [compact]
    return [compact[i:i + n] for i in range(len(compact) - n + 1)]


def embed_text(text: str | None, dim: int = DEFAULT_DIM) -> list[float]:
    """Return an L2-normalised embedding vector of length ``dim``."""
    if dim != DEFAULT_DIM:
        # keep the module usable with a custom dimension: scale bucket counts
        return _embed_scaled(text, dim)

    vector = [0.0] * DEFAULT_DIM
    if not text:
        return vector

    clean = sanitize_incoming_text(text, max_chars=6000)
    clean = _URL_RE.sub(" ", clean)
    normalized = normalize_text(clean)
    tokens = tokenize(clean)
    lowered = clean.lower()
    if not tokens and not normalized:
        return vector

    # 1. concept dimensions -------------------------------------------------
    for token in tokens:
        concept = _CONCEPT_INDEX.get(token) or _CONCEPT_INDEX.get(CRYPTO_ALIASES.get(token, token))
        if concept:
            idx = _hash_index(concept, _CONCEPT_BUCKETS, "concept")
            vector[idx] += 1.0
        # multi-word concept markers ("stop loss", "entry neoa")
        else:
            for marker, concept_name in _CONCEPT_INDEX.items():
                if " " in marker and marker in lowered:
                    idx = _hash_index(concept_name, _CONCEPT_BUCKETS, "concept")
                    vector[idx] += 0.5

    # 2. token dimensions ---------------------------------------------------
    for token in tokens:
        canonical = CRYPTO_ALIASES.get(token, token)
        if canonical in STOPWORDS:
            continue
        idx = _CONCEPT_BUCKETS + _hash_index(canonical, _TOKEN_BUCKETS, "token")
        vector[idx] += _hash_sign(canonical, "token") * (1.0 + min(len(canonical), 12) * 0.05)

    # 3. character n-gram dimensions (sub-word / morphology tolerant) -------
    for gram in _char_ngrams(normalized, 3):
        idx = _CONCEPT_BUCKETS + _TOKEN_BUCKETS + _hash_index(gram, _GRAM_BUCKETS, "gram")
        vector[idx] += _hash_sign(gram, "gram") * 0.6

    # NOTE: deliberately NO "language/script prior" dimensions here. A shared
    # constant component would make every Bangla memory look similar to every
    # Bangla question (cosine of two unrelated texts jumped to ~0.2 before this
    # was removed). Cross-script matching is handled by CONCEPT_GROUPS instead.

    vector = _l2_normalize(vector)
    return vector


def _embed_scaled(text: str, dim: int) -> list[float]:
    base = embed_text(text, DEFAULT_DIM)
    if dim <= 0:
        return []
    if dim == DEFAULT_DIM:
        return base
    if dim < DEFAULT_DIM:
        # fold the vector down deterministically
        folded = [0.0] * dim
        for i, value in enumerate(base):
            folded[i % dim] += value
        return _l2_normalize(folded)
    out = [0.0] * dim
    for i, value in enumerate(base):
        out[i] = value
    return _l2_normalize(out)


def _l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 1e-12:
        return [float(v) for v in vector]
    return [float(v) / norm for v in vector]


def cosine_similarity(a: Sequence[float] | None, b: Sequence[float] | None) -> float:
    """Cosine similarity for already-normalised (or raw) vectors."""
    if not a or not b:
        return 0.0
    size = min(len(a), len(b))
    if size == 0:
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for i in range(size):
        va = float(a[i] or 0.0)
        vb = float(b[i] or 0.0)
        dot += va * vb
        norm_a += va * va
        norm_b += vb * vb
    if norm_a <= 1e-12 or norm_b <= 1e-12:
        return 0.0
    return max(-1.0, min(1.0, dot / math.sqrt(norm_a * norm_b)))


def embed_many(texts: Iterable[str], dim: int = DEFAULT_DIM) -> list[list[float]]:
    return [embed_text(text, dim) for text in texts]


def vector_to_sql_literal(vector: Sequence[float] | None) -> str | None:
    """Render a vector as a PostgreSQL ``real[]`` literal (parameterised use)."""
    if not vector:
        return None
    return "{" + ",".join(f"{float(v):.6f}" for v in vector) + "}"


def concept_profile(text: str | None, limit: int = 6) -> list[str]:
    """Human readable summary of the concepts found in ``text`` (for logs)."""
    tokens = tokenize(text or "")
    found: list[str] = []
    for token in tokens:
        concept = _CONCEPT_INDEX.get(token) or _CONCEPT_INDEX.get(CRYPTO_ALIASES.get(token, token))
        if concept and concept not in found:
            found.append(concept)
        if len(found) >= limit:
            break
    return found

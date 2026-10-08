"""Text utilities tuned for Bangla / Banglish / English crypto chat.

These helpers are pure functions (no configuration, no I/O) so they are easy to
unit-test. They power:

* question detection (decides whether a member message is a question)
* keyword & crypto-term extraction (used by the retrieval search)
* topic classification (``qa_memory.topic`` / ``admin_messages.topic``)
* duplicate detection (keeps the AI context compact)
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from typing import Iterable, Sequence

# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff"), None)
_BENGALI_DIGITS = {ord(d): str(i) for i, d in enumerate("০১২৩৪৫৬৭৮৯")}
_ARABIC_DIGITS = {ord(d): str(i) for i, d in enumerate("٠١٢٣٤٥٦٧٨٩")}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WORD_RE = re.compile(r"[0-9A-Za-z\u0980-\u09FF]+")
_SPACE_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://\S+")

BENGALI_RANGE = re.compile(r"[\u0980-\u09FF]")


def translate_digits(text: str) -> str:
    return text.translate(_BENGALI_DIGITS).translate(_ARABIC_DIGITS)


def sanitize_incoming_text(text: str | None, max_chars: int = 8000) -> str:
    """Clean user input: strip control/zero-width chars, collapse whitespace."""
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", str(text))
    out = out.translate(_ZERO_WIDTH)
    out = _CONTROL_RE.sub(" ", out)
    out = _SPACE_RE.sub(" ", out).strip()
    if max_chars and len(out) > max_chars:
        out = out[:max_chars].rstrip()
    return out


def normalize_text(text: str | None) -> str:
    """Lower-cased, punctuation-free, digit-normalised form used for matching."""
    clean = sanitize_incoming_text(text)
    if not clean:
        return ""
    clean = translate_digits(clean.lower())
    tokens = _WORD_RE.findall(clean)
    return " ".join(tokens)


def tokenize(text: str | None) -> list[str]:
    return _WORD_RE.findall(normalize_text(text))


def strip_urls(text: str) -> str:
    return _URL_RE.sub(" ", text or "")


def html_escape(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def truncate(text: str | None, max_chars: int, suffix: str = "…") -> str:
    if not text:
        return ""
    text = str(text)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars <= len(suffix):
        return text[:max_chars]
    return text[: max_chars - len(suffix)].rstrip() + suffix


def truncate_middle(text: str, max_chars: int, suffix: str = "\n… (সংক্ষেপিত)") -> str:
    """Keep the head and the tail of long text (important parts of an answer)."""
    if not text or len(text) <= max_chars:
        return text or ""
    if max_chars <= len(suffix) + 40:
        return truncate(text, max_chars)
    budget = max_chars - len(suffix)
    head = int(budget * 0.6)
    tail = budget - head
    return text[:head].rstrip() + suffix + "\n" + text[-tail:].lstrip()


def sha256_short(text: str, length: int = 16) -> str:
    digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
    return digest[:length]


# --------------------------------------------------------------------------- #
# language detection
# --------------------------------------------------------------------------- #
BANGLISH_MARKERS = {
    "kemon", "kmn", "ki", "ki?", "keno", "kno", "kothay", "kothae", "kobe", "koto",
    "hobe", "hoy", "ache", "achhe", "nai", "naki", "bhai", "vai", "apu", "aktu",
    "ekhon", "akhon", "valo", "bhalo", "kharap", "korbo", "korle", "korben", "bujhi",
    "bolen", "bolo", "den", "dao", "jaben", "jabe", "niye", "nibo", "dibo", "khub",
    "onek", "kichu", "kemon", "ta", "to", "na", "hoyna", "hoyto", "lagbe", "mone",
    "mone hoy", "bujhlam", "dekhi", "dekhbo", "cholbe", "thik", "tik", "kotha",
    "jodi", "tahole", "tai", "ektu", "pore", "age", "sokal", "bikal", "raat",
}


def detect_language(text: str | None) -> str:
    """Return ``bn``, ``banglish``, ``en``, ``mixed`` or ``unknown``."""
    clean = sanitize_incoming_text(text)
    if not clean:
        return "unknown"
    letters = [ch for ch in clean if ch.isalpha()]
    if not letters:
        return "unknown"
    bengali = sum(1 for ch in letters if BENGALI_RANGE.match(ch))
    latin = sum(1 for ch in letters if ch.isascii())
    total = len(letters)
    ratio_bn = bengali / total
    ratio_en = latin / total
    tokens = set(tokenize(clean))
    banglish_hits = len(tokens & BANGLISH_MARKERS)

    if ratio_bn >= 0.5:
        return "bn"
    if ratio_en >= 0.9:
        return "banglish" if banglish_hits >= 2 else "en"
    if ratio_bn > 0.15 and ratio_en > 0.15:
        return "mixed"
    return "bn" if ratio_bn > ratio_en else ("banglish" if banglish_hits >= 2 else "en")


# --------------------------------------------------------------------------- #
# stopwords & keywords
# --------------------------------------------------------------------------- #
STOPWORDS: frozenset[str] = frozenset(
    """
    a an the am is are was were be been being do does did doing have has had having
    i me my mine we our ours you your yours he him his she her hers it its they them
    their this that these those and or but if then than so because as of to in on at
    by for with about from into over after before between during without within up down
    out off again further once here there when where why how all any both each few more
    most other some such no nor not only own same too very can will just should now
    what which who whom whose getting got please plz pls hi hello hey ok okay yeah yes
    thanks thank ok sir ji bro brother
    ami tumi apni tui apnara tomar amar apnar take take ta e etar eita oi oita ei eiটা
    ki ki? keno kno kemon kmn kothay kobe koto hobe hoy ache achhe nai naki holo hole
    korbo korle korben koreta kora korte kori kore korar kora jabe kora hobe
    na ni noi tay tai to je jeta ja jodi tahole kintu ar abar ekhon akhon akn
    onek khub valo bhalo kharap aktu ektu kichu sob shob kotha bolen bolo bolun
    sekhane ekhane amra tara o tader
    vai bhai vaiya bhaiya apu bondhu dost boro choto
    er ar der r te re ba ta ti gulo guli gule janabo lagbe lage lagen
    hocche hochhe hoche hoise holo hoye korte kore korle korar kora
    এখন যাবে হবে নেওয়া না কি তো রে যে আর এর ও এই সেই থেকে জন্য জনা আমি তুমি আপনি আমার আপনার
    একটা একটা কিছু বলুন বলবেন জানতে চাই আমি কী কেন কীভাবে কেমন কবে কোথায় কত
    """.split()
)

MIN_TOKEN_LEN = 2

#: Aliases -> canonical crypto ticker / term.
CRYPTO_ALIASES: dict[str, str] = {
    "bitcoin": "btc", "বিটকয়েন": "btc", "বিটকয়েনের": "btc", "bticoin": "btc", "xbt": "btc",
    "ethereum": "eth", "ইথেরিয়াম": "eth", "ether": "eth", "etherium": "eth",
    "solana": "sol", "binance coin": "bnb", "binancecoin": "bnb", "binance": "bnb",
    "ripple": "xrp", "cardano": "ada", "dogecoin": "doge", "doggy": "doge",
    "polkadot": "dot", "polygon": "matic", "avalanche": "avax", "chainlink": "link",
    "toncoin": "ton", "notcoin": "not", "pepe": "pepe", "shiba": "shib",
    "stop loss": "sl", "stop-loss": "sl", "stoploss": "sl", "স্টপ লস": "sl",
    "take profit": "tp", "take-profit": "tp", "takeprofit": "tp", "টেক প্রফিট": "tp",
    "support :": "support", "resistance :": "resistance", "mkt": "market",
    "liquidations": "liquidation", "liqs": "liquidation",
}

#: Terminology that matters for this group (requirement #11).
CRYPTO_TERMS: frozenset[str] = frozenset(
    """
    btc eth bnb sol xrp ada doge dot matic avax link ton not pepe shib usdt usdc btcusdt
    ethusdt solusdt bnb usd inr crypto cryptocurrency coin token altcoin alt meme
    spot futures future perpetual perp leverage margin cross isolated long short
    entry entries exit exits tp sl target takeprofit stoploss support resistance
    market marketstructure structure trend uptrend downtrend range breakout breakdown
    retest pump dump dumping candle timeframe 1m 5m 15m 1h 4h 1d weekly daily
    liquidity liquidation funding fundingrate openinterest oi volume volatility
    bullish bearish neutral correction accumulation distribution pullback dump
    dca averaging average risk reward rrr rr portfolio holding hodl spot buy sell
    scalp scalping swing intraday breakout confirmation fakeout fake breakout
    halving etf fed cpi inflation news listing airdrop presale launch pumpfun
    profit loss pnl roi apy staking mining whale pumpanddump
    """.split()
)

#: Keys used to guess a topic/category.
TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "entry_exit": ("entry", "entries", "exit", "tp", "sl", "target", "stoploss", "takeprofit",
                   "buy", "sell", "kine", "beche", "bikroy", "ক্রয়", "বিক্রয়"),
    "market_structure": ("support", "resistance", "structure", "trend", "uptrend", "downtrend",
                         "breakout", "breakdown", "range", "retest", "chart", "candle",
                         "timeframe", "pattern", "level", "zone"),
    "futures_leverage": ("futures", "future", "perp", "perpetual", "leverage", "long", "short",
                         "funding", "fundingrate", "openinterest", "oi", "margin", "liquidation",
                         "cross", "isolated", "liq"),
    "altcoin": ("altcoin", "alt", "meme", "airdrop", "listing", "presale", "launch", "token",
                "lowcap", "gem", "pumpfun"),
    "risk_management": ("risk", "reward", "rrr", "dca", "averaging", "capital", "sizing",
                        "portfolio", "holding", "hodl", "loss", "profit", "pnl", "roi"),
    "macro_news": ("fed", "cpi", "inflation", "etf", "news", "macro", "rate", "halving",
                   "regulation", "sec", "etf", "ratehike"),
    "psychology": ("fear", "greed", "panic", "mindset", "emotion", "discipline", "patience"),
    "education": ("learn", "shikha", "শিখ", "tutorial", "teach", "guide", "basics", "beginner"),
    "greeting": ("assalamu", "salam", "আসসালামু", "hello", "hi", "hey", "good morning",
                 "shubho", "শুভ", "গুড", "morning", "night", "vai", "bhai", "thanks", "ধন্যবাদ"),
}


def extract_keywords(text: str | None, max_keywords: int = 12) -> list[str]:
    """Domain-aware keyword extraction (stopwords + duplicates removed)."""
    tokens = tokenize(text)
    keywords: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        canonical = CRYPTO_ALIASES.get(token, token)
        if canonical in seen:
            continue
        if canonical in STOPWORDS and canonical not in CRYPTO_TERMS:
            continue
        if len(canonical) < MIN_TOKEN_LEN and not canonical.isdigit():
            continue
        seen.add(canonical)
        keywords.append(canonical)
        if len(keywords) >= max_keywords:
            break
    return keywords


def extract_crypto_terms(text: str | None) -> list[str]:
    """Return the crypto/trading vocabulary found in ``text``."""
    normalized_text = normalize_text(text)
    tokens = tokenize(text)
    found: list[str] = []
    seen: set[str] = set()
    # multi-word aliases such as "stop loss" -> "sl", "take profit" -> "tp"
    for alias, canonical in CRYPTO_ALIASES.items():
        if " " in alias and alias in normalized_text and canonical not in seen:
            seen.add(canonical)
            found.append(canonical)
    for token in tokens:
        canonical = CRYPTO_ALIASES.get(token, token)
        if canonical in CRYPTO_TERMS and canonical not in seen:
            seen.add(canonical)
            found.append(canonical)
    # uppercase tickers like "PEPE" or "SOLUSDT" that are not in the dictionary
    for raw in re.findall(r"\b[A-Z]{2,7}(?:USDT|USD|BTC|ETH)?\b", strip_urls(text or "")):
        canonical = raw.lower()
        if canonical in seen or canonical in STOPWORDS:
            continue
        if raw.endswith(("USDT", "USD")) or len(raw) in range(2, 6):
            seen.add(canonical)
            found.append(canonical)
    return found[:20]


def classify_topic(text: str | None) -> str:
    """Very light-weight topic classifier used for memory metadata."""
    keywords = set(extract_keywords(text, max_keywords=40)) | set(extract_crypto_terms(text))
    if not keywords:
        return "general"
    best_topic, best_score = "general", 0
    for topic, words in TOPIC_KEYWORDS.items():
        score = len(keywords & set(words))
        if score > best_score:
            best_topic, best_score = topic, score
    return best_topic


# --------------------------------------------------------------------------- #
# question detection
# --------------------------------------------------------------------------- #
QUESTION_CHARS = "?？"

BANGLA_INTERROGATIVES = {
    "কি", "কী", "কেন", "কেমন", "কবে", "কোথায়", "কোথা", "কে", "কার", "কত", "কতটা",
    "কতটুকু", "কোন", "কোনটা", "কোনটি", "নাকি", "তাই", "ঠিক", "হবে", "যাবে", "নিবো",
    "নেবো", "দিবো", "দেবো", "পারবো", "পারি", "উচিত", "দরকার", "লাগবে", "বলবেন",
    "বলেন", "বলো", "বলুন", "জানতে", "চাই", "চাইলে", "হয়", "হয়নি", "আছে", "নেই",
    "কিভাবে", "কীভাবে", "কতো", "কমনে", "কেমন", "উচিৎ",
}
ENGLISH_INTERROGATIVES = {
    "what", "when", "why", "how", "which", "who", "whom", "whose", "where",
    "can", "could", "should", "shall", "will", "would", "may", "might", "must",
    "is", "are", "am", "was", "were", "do", "does", "did", "has", "have", "had",
    "any", "possible", "possible?", "kemon", "keno", "kothay", "kobe", "koto",
}
BANGLISH_INTERROGATIVES = {
    "kemon", "kem", "keno", "kno", "kothay", "kothae", "kobe", "koto", "ki", "naki",
    "hobe", "hoy", "ache", "achhe", "parbo", "parmu", "uchit", "dorkar", "lagbe",
    "bolben", "bolen", "jante", "chai", "thik", "tik", "jabe", "nibo", "dibo", "kivabe",
    "kibhabe", "kemon", "koto", "kmn", "kivabe",
}
REQUEST_PATTERNS = (
    "বলেন", "বলবেন", "বলুন", "কিছু বলুন", "জানতে চাই", "জানাবেন", "জানতে", "হেল্প",
    "সাজেস্ট", "বুঝিয়ে", "কনফার্ম", "আপডেট", "analysis", "analysis ki", "দয়া করে",
    "please tell", "any idea", "any update", "advice", "suggest", "opinion", "মত কি",
    "মতামত", "পরামর্শ", "কি মনে", "কী মনে", "what about", "how about", "thoughts",
    "your view", "bolun", "bolo na", "janate chai", "help koro",
)
ACTION_PATTERNS = (
    "নেব কি", "নিব কি", "করব কি", "কিনব কি", "বেচব কি", "হবে কি", "যাবে কি", "কি হবে",
    "উচিত হবে", "ঠিক হবে", "safe কি", "risky কি", "vai ki", "bhai ki",
)


def is_question(text: str | None, *, threshold: float = 0.5) -> bool:
    """Heuristic question detector for Bangla / Banglish / English messages.

    Returns ``True`` when the message asks something (a question mark, an
    interrogative word in a typical position, or a request/advice pattern).
    """
    clean = sanitize_incoming_text(text)
    if not clean:
        return False
    if any(ch in clean for ch in QUESTION_CHARS):
        return True

    lower = clean.lower()
    for pattern in ACTION_PATTERNS:
        if pattern in lower:
            return True
    for pattern in REQUEST_PATTERNS:
        if pattern in lower:
            return True

    tokens = tokenize(clean)
    if not tokens:
        return False

    score = 0.0
    head = tokens[0]
    if head in BANGLA_INTERROGATIVES or head in BANGLISH_INTERROGATIVES:
        score += 0.6
    if head in ENGLISH_INTERROGATIVES and len(tokens) > 2:
        score += 0.5
    tail = tokens[-2:] if len(tokens) > 1 else tokens
    if any(token in BANGLA_INTERROGATIVES for token in tail):
        score += 0.45
    if any(token in BANGLISH_INTERROGATIVES for token in tail):
        score += 0.45
    # interrogative word anywhere in a short message: "মার্কেট কি উপরে যাবে"
    if len(tokens) <= 8 and any(
        token in BANGLA_INTERROGATIVES or token in BANGLISH_INTERROGATIVES
        for token in tokens[1:-1]
    ):
        score += 0.35
    if len(tokens) <= 6 and any(token in ENGLISH_INTERROGATIVES for token in tokens[1:]):
        score += 0.3
    canonical_tokens = {CRYPTO_ALIASES.get(token, token) for token in tokens}
    if canonical_tokens & CRYPTO_TERMS and (canonical_tokens & {
        "kemon", "hobe", "nibo", "entry", "kothay", "jabe", "কেমন", "যাবে", "নিবো",
    }):
        score += 0.3
    return score >= threshold


# --------------------------------------------------------------------------- #
# similarity (dedupe / retrieval scoring)
# --------------------------------------------------------------------------- #
def token_set(text: str | None) -> set[str]:
    return set(tokenize(text))


def jaccard(a: Iterable[str] | str, b: Iterable[str] | str) -> float:
    set_a = set(a) if not isinstance(a, str) else token_set(a)
    set_b = set(b) if not isinstance(b, str) else token_set(b)
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union else 0.0


def char_ngrams(text: str | None, n: int = 3) -> list[str]:
    normalized = normalize_text(text).replace(" ", "_")
    if not normalized:
        return []
    padded = f"_{normalized}_"
    if len(padded) <= n:
        return [padded]
    return [padded[i:i + n] for i in range(len(padded) - n + 1)]


def trigram_similarity(a: str | None, b: str | None) -> float:
    grams_a = set(char_ngrams(a))
    grams_b = set(char_ngrams(b))
    if not grams_a or not grams_b:
        return 0.0
    inter = len(grams_a & grams_b)
    return 2 * inter / (len(grams_a) + len(grams_b))


def text_similarity(a: str | None, b: str | None) -> float:
    """Combined lexical similarity in ``[0, 1]`` (token + character based)."""
    if not a or not b:
        return 0.0
    return max(jaccard(a, b), trigram_similarity(a, b))


def query_coverage(question_keywords: Sequence[str], candidate_text: str | None) -> float:
    """Fraction of question keywords present in the candidate (0..1)."""
    if not question_keywords or not candidate_text:
        return 0.0
    candidate_tokens = token_set(candidate_text)
    hits = sum(1 for kw in question_keywords if kw in candidate_tokens)
    return hits / len(question_keywords)

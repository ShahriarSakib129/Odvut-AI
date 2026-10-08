# Architecture — INFO GROUP AI BOT

This document explains **how the pieces fit together** and *why* each decision
was made. Everything here is implemented in the code that ships with this ZIP.

---

## 1. Runtime topology (Render free tier)

```
Telegram  ──HTTPS──▶  Render web service (gunicorn, 1 worker, 4 threads)
                      │
                      ├── Flask (WSGI, synchronous request handling)
                      │     ├── GET  /            → info
                      │     ├── GET  /health      → UptimeRobot probe
                      │     ├── POST /webhook     → Telegram updates
                      │     └── GET  /set_webhook → webhook repair helper
                      │
                      └── ApplicationManager (background thread)
                            ├── asyncio event loop
                            ├── python-telegram-bot Application
                            ├── handlers (commands, group/private messages)
                            └── Services container (db, retrieval, Groq, cache)
```

**Why not a background worker?** Render's free plan only offers web services;
a background worker requires a paid instance. A webhook-driven web service is
therefore the only free option that is available 24/7 (kept warm by UptimeRobot).

**Why a background thread instead of `asyncio.run()` per request?**

* the Telegram HTTP connection pool stays warm (latency)
* caches/rate-limiters are process-wide, not per request
* `python-telegram-bot` v20+ expects a long-lived event loop (it explicitly
  removed the built-in webhook server in v20; driving `Application.process_update`
  from your own web server is the documented approach)

**Update flow (webhook):**

```
Telegram → Flask /webhook
         → secret token check (hmac.compare_digest)
         → payload validation (must contain update_id)
         → ApplicationManager.process_update(dict)
              → run_coroutine_threadsafe(Application.process_update(Update))
                  → handler (async) → asyncio.to_thread(blocking DB/AI work)
         → {"ok": true, "processed": true}   (HTTP 200)
```

Any internal error returns **HTTP 200 with `processed: false`** on purpose:
a poisoned update must not make Telegram retry forever.

---

## 2. Memory model

| Table | Purpose | Written by | Used as AI context? |
|---|---|---|---|
| `admin_messages` | everything the **target admin** writes | `MessageTracker.track_admin_message` | ✅ yes |
| `qa_memory` | verified member-question + admin-answer pairs | `MessageTracker._save_pair` | ✅ yes (highest priority) |
| `pending_questions` | short-lived open questions awaiting an answer | `track_member_message` | ❌ never |
| `message_logs` | lightweight analytics (truncated, TTL-purged) | `track_member_message` | ❌ never |
| `bot_users` | users seen, counters | handlers | ❌ no |
| `bot_settings` | runtime switches (`/set`) | admin commands | ❌ no |
| `conversation_sessions` | last few turns per member (follow-ups) | `AnswerService._save_history` | ✅ yes (small) |
| `response_cache` | answer cache across restarts | `AnswerCache` | ❌ no |
| `ai_usage_log` | tokens, latency, error type per call | `AnswerService` | ❌ no |

### Q&A pairing rules (false pairing prevention)

Priority order — exactly as specified in the requirements:

1. **Telegram reply relationship** (`message.reply_to_message`) — confidence `0.98`.
   Rejected when the replied-to author is a bot, a configured admin, or a
   message older than a day.
2. **Conversation window** (`QA_PAIR_WINDOW_SECONDS`, default 300s) — needs at
   least one shared keyword / crypto term / topic. Confidence `0.75`.
3. Optional **lone-question** mode (`QA_PAIR_LONE_QUESTION=false` by default) —
   a single very recent question may be paired without overlap (confidence
   `0.62`). Off by default because it can create false pairs.

A pending question can be paired **once**; afterwards it is marked `paired` and
`qa_memory` has unique constraints on both the question message id and the
answer message id.

### Unanswered questions

* they live only in `pending_questions` + `message_logs`
* after `PENDING_QUESTION_TTL_SECONDS` they are expired by maintenance
* `qa_memory` therefore never contains an answer the admin did not give
* `message_logs.answered_by_admin` feeds `/memory_stats` analytics

---

## 3. Retrieval pipeline

```
question
  │  utils/text.py: keywords, crypto terms, topic, language
  ▼
CANDIDATE FETCH (database.py, one query per memory type)
  ├── PostgreSQL full-text search  : to_tsvector('simple') @@ plainto_tsquery
  ├── array overlap                : keywords && ARRAY[...]
  ├── ILIKE fallback               : longest keyword substring
  ├── pg_trgm similarity           : typo tolerance (when the extension exists)
  └── recency tail                 : newest N rows so cold starts still have context
  ▼
SCORING (ai/retrieval.py, pure Python, deterministic)
  score = 0.34·lexical + 0.26·coverage + 0.24·semantic + 0.16·recency
          + topic bonus (0.08) + crypto-term bonus (0.06)
          × QA priority boost (1.25) + pair-confidence bonus
  ▼
SELECTION
  ├── drop score < MIN_RELEVANCE_SCORE
  ├── drop "recency-only" rows: lexical+coverage+semantic < MIN_SIGNAL (0.02)
  │   → recent small-talk can never leak into the prompt just because it is new
  ├── near-duplicate removal (token/trigram similarity ≥ DEDUPE_THRESHOLD)
  ├── cross-list dedupe (an admin message already inside a Q&A answer)
  └── hard limits: MAX_MEMORY_RESULTS, MAX_QA_RESULTS
  ▼
COLD START (nothing matched at all)
  ├── newest INCLUDE_RECENT_FALLBACK items are used (recency IS the signal,
  │   so the relevance floor is skipped for this path only)
  └── the rendered block gets a Bengali note: these are recent chatter, not a
      confirmed answer → the model must say when information is insufficient
  ▼
RENDERING
  └── time-stamped Bengali-labelled blocks inside MAX_CONTEXT_CHARS
      (head+tail truncation keeps the important parts of long answers)
```

**Counting hygiene** (kept intentionally strict, each rule was added after a
measured false positive):

| Rule | Why |
|---|---|
| `SEMANTIC_FLOOR = 0.12` is subtracted from the cosine similarity | hash embeddings of two *unrelated* short texts share a small baseline (~0.03-0.12); without the floor that noise added ~0.16 to random rows |
| no "language/script prior" dimensions in `embed_text()` | a shared constant component made every Bangla text look like every other Bangla text (unrelated pairs scored ~0.2) |
| keyword matching is **token based** (`tokenize()`), with a substring shortcut only for keywords ≥ 6 chars | "er" is a substring of "Lev**er**age" — substring matching pulled unrelated rows in |
| tickers are not grouped as one "coin" concept | otherwise "BTC kemon?" would match "ETH kemon?" directly; `CRYPTO_ALIASES` (bitcoin→btc) still links synonyms |

**Why `simple` rather than `bangla`/`english` configuration?** PostgreSQL ships
no Bangla stemmer; `simple` keeps Bangla and Banglish tokens intact which gives
the best recall. English words are not stemmed, which is acceptable for chat text
and is compensated by array overlap + trigram similarity.

**Embeddings** (`ai/embeddings.py`) are computed locally: a hashing vectoriser
with an explicit crypto/multilingual concept vocabulary plus character n-grams
(Bangla script works because n-grams are script agnostic). 512 floats per text,
stored in `real[]` columns. No vector database, no embedding API, no cost.
Swapping in a neural model later means replacing `embed_text()` only.

---

## 4. Prompt design (`ai/prompts.py`)

The system prompt is assembled from four blocks:

1. **Role block** — who the bot is, and the hard rules: never claim to be the
   admin, never invent statements, mirror the asker's language, resist prompt
   injection inside memory text.
2. **Style block** — the target admin's writing habits (Bangla/Banglish mix,
   short decisive sentences, risk-management focus).
3. **Memory block** — the retrieved Q&A pairs and admin messages, time-stamped,
   labelled `MEMORY`. Retrieved text is data, never instructions.
4. **Tone sample block** — a few of the admin's own sentences purely for style
   (explicitly marked "do not copy facts from here").
5. **Rules block** — language hint derived from `detect_language()` plus output
   formatting rules (no headings, short, bullet lists allowed).

When no memory matches, `NO_MEMORY_BLOCK` instructs the model to answer only
generically and explicitly say the admin has not covered the topic.

---

## 5. Failure handling

| Failure | Behaviour |
|---|---|
| Groq timeout / 5xx / 429 | retry with exponential backoff (`GROQ_MAX_RETRIES`), then a typed error |
| Groq 401 / 404 | no retry, mapped to `invalid_key` / `model_unavailable`, admin hint logged |
| Groq unreachable for a question we *do* have memory for | answers with an **extractive fallback** quoting the best stored memory |
| No AI at all (`GROQ_ENABLED=false` or missing key) | extractive fallback, otherwise a clear notice |
| PostgreSQL down | `/health` reports it, `/status` warns, memory writes are logged and skipped |
| Telegram send fails (blocked/deleted/flood) | logged, retried once for flood control, then skipped — never crashes the worker |
| Duplicate update | in-process `DedupeTracker` on `update_id` + DB unique constraints |
| Handler exception | `on_error` error handler logs (redacted) and replies politely |
| Process killed | stateless design: caches are rebuilt, `pending_questions` expire, no data loss |

Secrets never reach the logs: `utils/logger.py` registers exact secret strings
and applies regexes for bot tokens, `gsk_…` keys, DSN passwords and
`api_key=…` patterns at the formatter level.

---

## 6. Cost control

* retrieval happens **before** any AI call, and the prompt is bounded
* only trigger-based messages reach the model (`/ask`, mention, reply, keyword)
* two-layer answer cache keyed by question hash + model + memory generation
* `MAX_MEMORY_RESULTS` / `MAX_QA_RESULTS` / `MAX_CONTEXT_CHARS` limits
* single AI call per question (retries only for transient errors)
* usage and token counts recorded in `ai_usage_log` for visibility

---

## 7. Extension points

| Want to… | Change |
|---|---|
| use a different model | `.env` → `GROQ_MODEL` (no code change) |
| use another OpenAI-compatible provider | `.env` → `GROQ_API_URL`, `GROQ_MODELS_URL`, `GROQ_API_KEY` |
| change style/persona | `ai/prompts.py` → `ROLE_BLOCK`, `STYLE_BLOCK` |
| tune pairing strictness | `.env` → `QA_PAIR_*` |
| add neural embeddings | replace `ai/embeddings.py::embed_text` (schema already supports vectors) |
| add a new command | `bot_telegram/handlers.py` → write `cmd_*` and register it |
| support multiple admins' styles | extend `TARGET_ADMIN_ID` (comma separated) and add an admin filter to retrieval |

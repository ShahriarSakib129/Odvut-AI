# Troubleshooting — INFO GROUP AI BOT

Start here:

```bash
python scripts/check_config.py --online
curl https://<your-service>.onrender.com/health
```

Those two commands identify most problems. Details below.

---

## A. Bot does not answer at all

| Check | How | Fix |
|---|---|---|
| Is the bot alive? | `/ping` in Telegram | if no reply → webhook/service problem (section B) |
| Privacy mode | BotFather → `/setprivacy` | must be **Disable**, then remove+re-add the bot to the group |
| Right group | `/whoami` → `chat id` | must equal `GROUP_ID` |
| Trigger | — | bot only answers `/ask`, mentions, replies to it, or `TRIGGER_KEYWORDS` |
| Auto-reply switch | `/adminhelp` | `auto_reply_enabled` must be `on` |
| AI switch | `/status` | `ai_enabled` must be `on` and AI must show ✅ |
| Rate limit | — | max `RATE_LIMIT_PER_MINUTE` (default 6) answers per member per minute |

## B. Webhook problems

**Symptom:** Telegram shows no activity, `/set_webhook` returns an error,
`/health` shows `telegram.started: false`.

1. Confirm the public URL:
   ```
   https://<your-service>.onrender.com/health
   ```
2. Register/repair the webhook:
   ```
   https://<your-service>.onrender.com/set_webhook?token=<WEBHOOK_SECRET>
   ```
   or locally `python scripts/set_webhook.py --set --delete-first`, then
   `python scripts/set_webhook.py --info`.
3. `last_error_message` in the webhook info tells you exactly what Telegram thinks:
   * `Wrong response from the webhook: 403 Forbidden` → `WEBHOOK_SECRET` mismatch
     (the value in Render must equal the one used when registering)
   * `Wrong response from the webhook: 502/503` → the service was sleeping or
     crashing; check Render logs
   * `SSL error` / `connection refused` → `PUBLIC_URL` wrong (must be `https://…`)
4. Deployed but the webhook never got set? Startup registration needs
   `SET_WEBHOOK_ON_STARTUP=true` **and** `PUBLIC_URL`. You can always call the
   `/set_webhook?token=…` endpoint once manually.
5. Using polling locally and getting `Conflict: terminated by other getUpdates`?
   A webhook is still registered:
   ```bash
   python scripts/set_webhook.py --delete
   python bot.py
   ```

## C. Database problems

| Message | Cause | Fix |
|---|---|---|
| `DATABASE_URL is missing` | env var not set | add it (Render → Environment) |
| `database: connection failed` | wrong password / host / IP restriction | copy the URI again from Supabase, percent-encode special characters |
| `missing tables: qa_memory, …` | schema not applied | Supabase SQL Editor → run `database/schema.sql` |
| `invalid connection option "pgbouncer"` | stale client code | `pip install -r requirements.txt` (current code strips unknown params) |
| `remaining connection slots are reserved` | pool too large / too many clients | set `DB_POOL_MAX=2`, or use Supabase's *Session pooler* URI |
| `canceling statement due to statement timeout` | slow query on a huge table | raise `DB_STATEMENT_TIMEOUT_MS`, run `python scripts/maintenance.py --vacuum` |
| Statistics show 0 users forever | bot never received updates | section B |

## D. AI / Groq problems

| Symptom | Meaning | Fix |
|---|---|---|
| `/status` shows `AI: ⛔ disabled` | key missing or `GROQ_ENABLED=false` | set `GROQ_API_KEY`, or `/set ai_enabled on` + `GROQ_ENABLED=true` |
| `invalid_key` in logs | bad/expired key | create a new key at console.groq.com |
| `model_unavailable` | model renamed/retired | change `GROQ_MODEL` (e.g. `openai/gpt-oss-20b`, `llama-3.3-70b-versatile`) |
| `rate_limited` | free tier limit | wait, or enable caching (`CACHE_ENABLED=true`), reduce `GROQ_MAX_TOKENS` |
| `timeout` | Groq slow / big prompt | lower `MAX_CONTEXT_CHARS`, `GROQ_MAX_TOKENS`; raise `GROQ_TIMEOUT_SECONDS` |
| Answers ignore the admin's memory | retrieval found nothing | `/memory <question>` shows the match scores; consider lowering `MIN_RELEVANCE_SCORE` to `0.03` |
| Answers are too generic | not enough memory yet | keep chatting as the admin; the memory grows automatically |
| Answers sound nothing like the admin | needs prompt tuning | edit `ai/prompts.py` → `STYLE_BLOCK` |

Extractive fallback (answers begin with "AI service এখন unavailable…") is normal
when Groq is unreachable — the bot quotes stored memory instead of failing.

## E. Memory / Q&A pairing problems

| Symptom | Fix |
|---|---|
| Admin messages are not stored | `TARGET_ADMIN_ID` must match the real admin; `/whoami` to verify; privacy mode must be **Disable** |
| Q&A pairs are not created | the admin must **reply** to the member's question (most reliable), or the answer must arrive within `QA_PAIR_WINDOW_SECONDS` and share a keyword |
| Wrong pairs being created | lower `QA_PAIR_WINDOW_SECONDS` (e.g. 120), set `QA_PAIR_REQUIRE_QUESTION=true`, keep `QA_PAIR_LONE_QUESTION=false` |
| Unanswered questions counted as memory | they are not — check `/memory_stats`; `qa_memory` only stores admin-answered questions |
| Memory suddenly empty | someone ran `/clear_memory … confirm`; the `ai_usage_log`/Render logs record it |
| Storage growing fast | `LOG_TEXT_CHARS=200`, `TRACK_ALL_MEMBER_MESSAGES=false`, run `scripts/maintenance.py` |

## F. Render-specific

| Symptom | Fix |
|---|---|
| Service sleeps, first request slow | add the UptimeRobot monitor (5 min) |
| `Build failed` | check the `pip install` log. Python version: Render ignores `runtime.txt` and reads `.python-version` (`3.11`); if you pinned a bad version with the `PYTHON_VERSION` env var, remove it |
| `Network is unreachable` / `no tenant identifier provided` | you are using Supabase's **Direct connection** (IPv6-only) — switch to the **Session pooler** URI (port 5432, user `postgres.[PROJECT-REF]`). See `docs/DEPLOYMENT.md` |
| `Application failed to respond` | gunicorn start command must be exactly the one in the README (or `Procfile`) |
| Deploy succeeds but bot is dead | Render → Logs: look for `configuration problems detected: (...)` and fix those env vars |
| Restart loop (crash on boot) | wrong `BOT_TOKEN` format, or `DATABASE_URL` empty and `WEBHOOK_REQUIRE_SECRET=true` without `WEBHOOK_SECRET` |
| Want to switch to polling | Start Command `python bot.py`, `PUBLIC_URL` empty, `python scripts/set_webhook.py --delete` first (needs a paid instance) |

## G. Testing locally

```bash
pytest -q                                    # unit tests (DB tests skip if unset)
TEST_DATABASE_URL="postgresql://..." pytest -q   # everything, including SQL
python scripts/simulate_conversation.py -v   # offline pipeline demo
python app.py                                # Flask on :5000
curl -s localhost:5000/health | python -m json.tool
```

## H. Reading the logs

Useful log lines and what they mean:

| Line | Meaning |
|---|---|
| `services built: ready=True errors=none` | startup succeeded |
| `database: connected (PostgreSQL 15.x), pg_trgm=True, missing tables=none` | schema OK |
| `admin memory stored: admin=… pairing=True` | admin message saved (and paired) |
| `Q&A memory created: id=… method=reply confidence=0.98` | pair stored |
| `member question tracked: … pending=12` | question awaiting an answer |
| `retrieval: selected 6 admin memories + 3 Q&A pairs (4120 chars, 190 candidates)` | retrieval worked |
| `groq OK model=openai/gpt-oss-120b latency=890ms tokens=420/120` | AI answered |
| `answer served from cache (trace=…)` | no AI call (cost saved) |
| `groq failed (trace=… kind=rate_limited)` | vendor-side problem, fallback used |
| `rejected webhook call with bad/missing secret` | wrong `WEBHOOK_SECRET` |
| `duplicate update skipped` | Telegram redelivery — harmless |

Set `LOG_LEVEL=DEBUG` in Render for verbose output (never logs secrets).

---

## The bot is silent: run the doctor first

```
GET https://<your-service>.onrender.com/diagnose?token=<WEBHOOK_SECRET>
```

It checks, in one request: configuration, PostgreSQL, `getMe` (including
**privacy mode**), `getWebhookInfo` (registered URL + Telegram's `last_error_message`),
group membership and the slash-commands, and returns Bengali fix hints.

The three failures it reports most often:

| Symptom | Cause | Fix |
|---|---|---|
| `last_error_message` contains **404 Not Found**, registered URL ends with `/webhook/webhook` | `PUBLIC_URL` was set to the full webhook URL instead of the base URL | Render → Environment → `PUBLIC_URL` = `https://<service>.onrender.com` (no path), then open `/set_webhook?token=...` |
| `last_error_message` contains **403 Forbidden** | Telegram was registered without `secret_token`, or `WEBHOOK_SECRET` changed | open `/set_webhook?token=<current WEBHOOK_SECRET>` — it re-registers with the secret |
| `privacy_mode: ENABLED` | BotFather privacy mode is on, so the bot never receives normal group messages | `/setprivacy` → **Disable** → remove the bot from the group and add it again |

> Setting the webhook **before or after** the deploy makes no difference: the bot
> re-registers it on every startup (`SET_WEBHOOK_ON_STARTUP=true`). Only the URL
> and the secret have to match.

---

## Webhook hits while the bot is cold-starting

Render sleeps free services. The first webhook after a cold start can arrive
before `Application.initialize()` has finished, so the endpoint answers:

```json
{"ok": false, "reason": "bot starting", "processed": false}   # HTTP 503
```

That is intentional: **503 tells Telegram to deliver the update again**, so the
question is not lost. In the same request the app already kicks the background
start, and `GET /health` (UptimeRobot, every 5 minutes) does the same, which is
why a sleeping bot wakes up before Telegram gives up. If you see 503 repeatedly
in `getWebhookInfo`, check `/health` → `telegram.start_error`.

## The bot answers "এই তথ্য আমার কাছে নেই" although a memory exists

The retrieval step is deliberately conservative. Reasons, in order of likelihood:

1. the question and the memory share **no** keyword/concept (different ticker,
   different wording) → the fallback block is marked as *recent chatter* and the
   model is instructed not to invent an answer,
2. the memory is only in `pending_questions` (the admin reply was never paired),
3. `ai_enabled` / `memory_enabled` was switched off with `/set`,
4. the memory is older than `RECENCY_HALF_LIFE_DAYS` and below `MIN_RELEVANCE_SCORE`.

Check what the bot actually retrieved with `/memory <প্রশ্ন>` (admin) or
`/memory_stats`, and lower `MIN_RELEVANCE_SCORE` only if you really want looser
matching.

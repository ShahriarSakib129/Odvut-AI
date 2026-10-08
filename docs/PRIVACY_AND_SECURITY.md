# Privacy & security — INFO GROUP AI BOT

This bot stores group messages in a database and sends selected text to an AI
provider. That is a **privacy-relevant** design, so this document states exactly
what happens — please share the summary with your group members.

---

## 1. What is stored, and why

| Data | Table | Retention | Used for AI answers? |
|---|---|---|---|
| Messages of the **target admin** (text + metadata + embedding) | `admin_messages` | until cleared | ✅ yes — this is the knowledge base |
| Member question **only if the admin answered it**, together with the admin's answer | `qa_memory` | until cleared | ✅ yes — highest priority context |
| Questions awaiting an answer | `pending_questions` | `PENDING_QUESTION_TTL_SECONDS` (30 min default) | ❌ never |
| Lightweight message log (text truncated to `LOG_TEXT_CHARS`, default 600 chars) | `message_logs` | 30 days (maintenance) | ❌ never |
| Users seen (id, username, counters) | `bot_users` | until cleared | ❌ no |
| Runtime switches | `bot_settings` | permanent | ❌ no |
| Last conversation turns per member (for follow-up questions) | `conversation_sessions` | 30 minutes | ✅ small, current member only |
| Generated answers (cache) | `response_cache` | `CACHE_TTL_SECONDS` (15 min default) | ❌ no (it *is* an answer) |
| Token/latency/error counters | `ai_usage_log` | operational | ❌ no |

**Not stored:** other bots' messages, other admins' conversations (admin→admin
replies are never paired), member messages that are not questions (unless
`TRACK_ALL_MEMBER_MESSAGES=true`), any media file (only captions).

**Unanswered member questions never become AI memory.** They exist only as an
expiring pending row plus a truncated analytics entry.

## 2. What is sent to the AI provider (Groq)

For each answered question the bot sends:

1. the member's question,
2. the top-ranked memory items (max `MAX_MEMORY_RESULTS` admin messages +
   `MAX_QA_RESULTS` Q&A pairs), truncated to fit `MAX_CONTEXT_CHARS`,
3. the last few turns of *that member's* session (max 4 turns),
4. the system prompt (rules + style).

It never sends the whole database, other chats, usernames, user ids, chat ids,
or any credential. Groq's own privacy policy applies to this traffic
(<https://groq.com/privacy-policy/>).

Optional hardening: set `GROQ_ENABLED=false` to disable AI generation entirely —
the bot then only quotes stored memory (extractive mode).

## 3. Who can see or modify the data

* Only the process holding `DATABASE_URL` (your Render service) can read it.
* Supabase tables have **Row Level Security enabled with no policies**, so the
  anonymous PostgREST API (the public `anon` key) cannot read them. The bot uses
  the direct PostgreSQL connection, which bypasses RLS.
* Admin commands (`/stats`, `/memory_stats`, `/memory`, `/clear_memory`, `/set`)
  require `ADMIN_ID` (or, if you explicitly enable `ALLOW_GROUP_ADMINS=true`,
  a group administrator inside the configured group).
* Every destructive command requires the literal word `confirm`:
  `/clear_memory all confirm`.
* Admin commands from an unknown user are refused and logged.

## 4. Deleting data

| Want to delete | Command |
|---|---|
| Q&A pairs + pending questions | `/clear_memory qa confirm` |
| Admin memory (all `admin_messages`) | `/clear_memory admin confirm` |
| Analytics log only | `/clear_memory logs confirm` |
| Cached answers only | `/clear_memory cache confirm` |
| Everything above | `/clear_memory all confirm` |

Also available: `python scripts/maintenance.py` (expires questions, purges logs
older than 30 days) and a full backup/export via
`python scripts/export_memory.py --out exports/memory.json`.

To wipe a whole deployment: delete the Supabase tables or the project, and
remove the Render service.

## 5. Secret handling

* Nothing is hard-coded: tokens, keys and the database URL come from environment
  variables / `.env` (which is in `.gitignore`).
* `config.py` validates values and never prints them; `public_summary()` exposes
  only booleans/redacted forms (`postgresql://user:***@host/...`).
* `utils/logger.py` installs a formatter-level redactor that replaces registered
  secrets plus anything matching token/key/DSN patterns — including inside
  exception tracebacks.
* The Groq API key is only sent in the `Authorization: Bearer …` header to
  `api.groq.com`. Telegram's token only to `api.telegram.org`.
* Webhook calls are authenticated with the Telegram secret token
  (`X-Telegram-Bot-Api-Secret-Token`) compared with `hmac.compare_digest`.
  The `/set_webhook` endpoint is protected by the same secret.

## 6. Application security

* **SQL injection:** every query uses parameterised placeholders (`%(name)s`).
  The only interpolated identifiers come from hard-coded whitelists
  (`_SAFE_IDENTIFIER`, counter names, table names in admin-only helpers).
* **Input validation:** all Telegram text passes through
  `utils.text.sanitize_incoming_text` (control/zero-width characters removed,
  length capped before storage and before prompting).
* **Prompt injection:** retrieved memory is inserted as *data* inside a fenced
  behaviour block; the system prompt explicitly forbids treating it as
  instructions.
* **Output safety:** `sanitize_answer()` strips markdown headings/prefixes,
  caps the length, and flags any attempt to claim to be the admin (which forces
  the AI disclosure footer to be added).
* **Abuse control:** per-user rate limit, `MAX_QUESTION_CHARS`, and a hard cap on
  prompt size.
* **Exception leakage:** handlers and the Flask app log the exception (redacted)
  and return generic messages; no traceback reaches a user or an HTTP response.
* **Duplicate updates:** de-duplicated by `update_id` plus database unique
  constraints, so a Telegram retry can never create two memories.

## 7. Recommendations for your group

1. Tell members that the bot stores the admin's messages and admin-answered
   questions, and that answers are AI-generated.
2. Keep the AI disclosure footer enabled (`AI_DISCLOSURE_IN_GROUP=true`).
3. Tell members not to post personal, financial or sensitive details — the bot
   stores what it sees.
4. Use a dedicated Supabase project (not shared with other apps) and rotate
   `WEBHOOK_SECRET` if it ever leaks.
5. Review `/memory` occasionally: what the bot "thinks" is exactly what is stored.

# Setup checklist — INFO GROUP AI BOT

Print/copy this list and tick items off in order. Each item links to the detailed
instructions in `README.md`.

## Phase 1 — Local preparation

- [ ] Python 3.11+ installed (`python --version`)
- [ ] Project extracted / cloned, terminal inside `info-group-ai-bot/`
- [ ] Virtual environment created and activated (`.venv`)
- [ ] `pip install -r requirements.txt` succeeded
- [ ] `cp .env.example .env` done

## Phase 2 — Accounts and credentials

- [ ] Telegram bot created with **@BotFather** → `/newbot`
- [ ] Bot token copied
- [ ] Groq account created → API key created (starts with `gsk_`)
- [ ] Supabase project created (region close to you, DB password saved)
- [ ] Supabase connection string (Session pooler URI) copied
- [ ] `WEBHOOK_SECRET` generated (`python -c "import secrets;print(secrets.token_urlsafe(32))"`)

## Phase 3 — Fill in `.env`

- [ ] `BOT_TOKEN=` bot token
- [ ] `GROQ_API_KEY=` Groq key
- [ ] `GROQ_MODEL=openai/gpt-oss-120b` (or another Groq model)
- [ ] `DATABASE_URL=` Supabase URI
- [ ] `ADMIN_ID=` your Telegram user id
- [ ] `TARGET_ADMIN_ID=` the admin whose messages become memory
- [ ] `GROUP_ID=` your group id (negative number)
- [ ] `WEBHOOK_SECRET=` the generated secret
- [ ] `PUBLIC_URL=` left empty for now (filled after deployment)

## Phase 4 — Database

- [ ] Supabase → SQL Editor → `database/schema.sql` pasted → **Run** succeeded
- [ ] `python scripts/apply_schema.py` also returns OK (verifies connectivity)
- [ ] `python scripts/check_config.py` reports “all required tables exist”

## Phase 5 — Local verification

- [ ] `python scripts/simulate_conversation.py -v` shows retrieval + answer
- [ ] `pytest -q` → all tests pass (or database tests skip cleanly)
- [ ] `python bot.py` starts and `/ping` in Telegram answers 🏓
- [ ] `/whoami` in the group returns the expected user id and chat id
- [ ] Admin sends a message, then `/memory` shows it

## Phase 6 — Group configuration

- [ ] Bot added to the Telegram group
- [ ] BotFather → `/setprivacy` → bot → **Disable**
- [ ] Bot removed from the group and added again (Telegram applies the new privacy setting on re-join)
- [ ] Group admin writes a message → `/memory` shows it (memory is flowing)
- [ ] Member asks `/ask …` → the bot answers

## Phase 7 — Deployment (Render)

- [ ] Project pushed to GitHub (`.env` not committed)
- [ ] Render Web Service created (Python 3, Free, Singapore)
- [ ] Build command: `pip install --upgrade pip && pip install -r requirements.txt`
- [ ] Start command: `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --access-logfile - --error-logfile - app:app`
- [ ] All environment variables added (`PUBLIC_URL=https://<service>.onrender.com`)
- [ ] Deployment finished without build errors

## Phase 8 — Post-deployment

- [ ] `https://<service>.onrender.com/health` → `"status": "ok"`
- [ ] `/health` shows `"database": {"available": true}`
- [ ] `/health` shows `"telegram": {"started": true}`
- [ ] Webhook registered: `https://<service>.onrender.com/set_webhook?token=<WEBHOOK_SECRET>`
- [ ] UptimeRobot monitor created (HTTP, 5-minute interval, `/health`)
- [ ] `/status` in Telegram → 🟢 running, DB connected, AI ✅
- [ ] `/ask` question answered after deployment

## Phase 9 — Feature verification

- [ ] Admin message stored (`/memory`)
- [ ] Member question → admin **reply** → `/memory` shows a Q&A pair
- [ ] `/ask` a similar question → the answer uses the stored Q&A (mention style/content)
- [ ] Unanswered question → `/memory_stats` lists it as unanswered, `/memory` has no pair
- [ ] Same question twice → second answer comes from cache (`/stats` unchanged AI call count)
- [ ] Member cannot run `/stats` or `/clear_memory` (⛔ message)
- [ ] Admin runs `/clear_memory qa` → sees the confirmation preview, then `/clear_memory qa confirm`
- [ ] `/set auto_reply_enabled off` → group auto-answers stop, memory keeps collecting
- [ ] `/set auto_reply_enabled on` re-enables answers
- [ ] `/reload` after changing `.env` (local) shows the updated model/config

## Phase 10 — Housekeeping (optional, monthly)

- [ ] `python scripts/maintenance.py --vacuum`
- [ ] `python scripts/export_memory.py --out exports/memory.json` (backup)
- [ ] Review `/stats`: unanswered questions, token usage, DB latency
- [ ] Rotate `WEBHOOK_SECRET` if it was ever exposed

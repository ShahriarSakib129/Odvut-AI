# Deployment guide — Render + Supabase + Groq + UptimeRobot

Follow the steps in order. Everything below can be done with free accounts.

---

## 0. Prerequisites checklist

- [ ] GitHub account with this project pushed
- [ ] Telegram bot token (`@BotFather`)
- [ ] Groq API key (<https://console.groq.com/keys>)
- [ ] Supabase project + `DATABASE_URL`
- [ ] Render account (GitHub login)

---

## 1. Push the project to GitHub

```bash
cd info-group-ai-bot
git init
git add .
git commit -m "INFO GROUP AI BOT v1.0.0"
git branch -M main
git remote add origin https://github.com/<USER>/info-group-ai-bot.git
git push -u origin main
```

Verify that the secret file is **not** part of the repository:

```bash
git status --short          # .env must not be listed
git ls-files | grep -c "^.env$"   # must print 0
```

---

## 2. Create the Render web service

**Dashboard → New + → Web Service → connect the repository**

| Field | Value |
|---|---|
| Name | `info-group-ai-bot` |
| Region | `Singapore` (lowest latency from South Asia) |
| Branch | `main` |
| Runtime | `Python 3` |
| Build Command | `pip install --upgrade pip && pip install -r requirements.txt` |
| Start Command | `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --access-logfile - --error-logfile - app:app` |
| Instance Type | `Free` |
| Health Check Path | `/health` |

> **Alternative deployment (Blueprint):** if you prefer, push the repository and
> choose **New + → Blueprint**; `render.yaml` already contains this configuration
> (including `WEBHOOK_SECRET: generateValue: true`).

### Alternative start command (long polling — not recommended on free tier)

```bash
python bot.py
```
Use this only with a paid instance that does not sleep; leave `PUBLIC_URL` empty
so the bot does not try to register a webhook.

---

## 3. Environment variables

Render → your service → **Environment** → *Add Environment Variable*:

| Key | Value | Notes |
|---|---|---|
| `PYTHON_VERSION` | `3.11.9` | optional, `runtime.txt` already sets it |
| `BOT_TOKEN` | `123456789:AAH...` | from BotFather |
| `GROQ_API_KEY` | `gsk_...` | from Groq console |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | change any time |
| `DATABASE_URL` | `postgresql://postgres.xxx:pw@...pooler.supabase.com:5432/postgres` | Supabase URI |
| `ADMIN_ID` | `123456789` | your Telegram id |
| `TARGET_ADMIN_ID` | `123456789` | memory source |
| `GROUP_ID` | `-1001234567890` | your group |
| `PUBLIC_URL` | `https://info-group-ai-bot.onrender.com` | no trailing slash |
| `WEBHOOK_SECRET` | random string | `python -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `SET_WEBHOOK_ON_STARTUP` | `true` | registers the webhook automatically |
| `LOG_LEVEL` | `INFO` | use `DEBUG` while debugging |
| `ENVIRONMENT` | `production` | |

Optional but useful: `WEBHOOK_REQUIRE_SECRET=true` (default), `DROP_PENDING_UPDATES=true`
(default), `MAX_MEMORY_RESULTS=10`, `MAX_QA_RESULTS=5`, `MAX_CONTEXT_CHARS=12000`,
`CACHE_ENABLED=true`, `DB_POOL_MAX=4`, `RATE_LIMIT_PER_MINUTE=6`.

> **Password special characters:** if your Supabase password contains `@ : / ? #`,
> percent-encode it in the URL (e.g. `@` → `%40`). Otherwise the connection string
> will be parsed incorrectly.

---

## 4. Verify the deployment

1. Open `https://<your-service>.onrender.com/health`
   ```json
   {"status": "ok", "service": "INFO GROUP AI BOT", "database": {"available": true}, ...}
   ```
   * `"status": "degraded"` → read the `problems` array (missing env var).
   * `"database": {"available": false}` → `DATABASE_URL` wrong, or schema not applied.
2. Open `https://<your-service>.onrender.com/` — should return service info.
3. Check the logs (Render → **Logs**): you should see
   `database: connected`, `services built: ready=True`, `Telegram application started`.
4. Confirm the webhook:
   ```
   https://<your-service>.onrender.com/set_webhook?token=<WEBHOOK_SECRET>
   ```
   Expected: `{"ok": true, "description": "Webhook was set"}`

---

## 5. Apply the database schema (if not done yet)

Supabase → **SQL Editor** → paste `database/schema.sql` → **Run**.

Or locally:

```bash
DATABASE_URL="postgresql://..." python scripts/apply_schema.py
```

Verify:

```bash
DATABASE_URL="postgresql://..." python scripts/check_config.py
```

---

## 6. UptimeRobot (keeps the free instance awake)

1. <https://uptimerobot.com> → sign up (free)
2. **Add New Monitor**
   * Type: `HTTP(s)`
   * Name: `INFO GROUP AI BOT`
   * URL: `https://<your-service>.onrender.com/health`
   * Interval: `5 minutes`
   * Alert contact: your email
3. Save. The monitor should turn **Up** within a few minutes.

Without this, Render suspends the free service after ~15 minutes of inactivity
and Telegram deliveries will fail while it cold-starts (~50 s).

---

## 7. Post-deployment checks in Telegram

```
/ping        → 🏓 পং!
/whoami      → your user id and chat id (must match ADMIN_ID / GROUP_ID)
/status      → 🟢 running, database connected, model openai/gpt-oss-120b
/ask BTC এখন কেমন?   → an answer (or a clear "not enough information" notice)
/stats       → admin only
```

Then create a real memory:

1. Target admin writes: `BTC এখন support এর দিকে আছে, confirmation ছাড়া entry না নেওয়া ভালো`
2. A member asks: `/ask BTC এ entry নেওয়া যাবে?`
3. Admin replies to that question: `এখন না, confirmation আসুক`
4. `/memory` → both the admin message and the Q&A pair are visible.

---

## 8. Updating the deployment

```bash
git add .
git commit -m "tune retrieval"
git push
```

Render auto-deploys (`autoDeploy: true`). After changing environment variables
Render restarts the service automatically; webhook registration re-runs on
startup (`SET_WEBHOOK_ON_STARTUP=true`).

---

## 9. Switching to a different region / paid plan

If you upgrade: increase `DB_POOL_MAX` (e.g. 8) and consider `--workers 2` only if
you set `WEBHOOK_MAX_CONNECTIONS` appropriately — but remember that in-process
caches/rate limiters are per worker, so `1` worker remains the recommended setup
for correctness.

---

## 10. Rollback

Render keeps previous deploys: **Dashboard → your service → Deploys → ⋯ → Rollback**.
Database changes are additive (schema is idempotent) so a rollback usually needs
no SQL work.

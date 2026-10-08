# 🤖 INFO GROUP AI BOT

একটি production-ready Telegram Group AI Bot, যা একটি নির্দিষ্ট **Admin**-এর লেখার স্টাইল, ব্যাখ্যা, opinion এবং আগের উত্তর থেকে context নিয়ে Member-দের প্রশ্নের উত্তর দেয় — **বাংলা / Banglish / English** ভাষায়।

> ⚠️ Bot কখনো দাবি করে না যে সে আসল Admin। Database/context-এ তথ্য না থাকলে সে বানিয়ে বলে না — স্পষ্টভাবে জানায় যে নিশ্চিত হওয়া যাচ্ছে না।

```
Member question ──▶ Retrieval (keyword + full-text + semantic) ──▶ compact context ──▶ Groq AI ──▶ Admin-স্টাইলে উত্তর
                        ▲
      admin_messages + qa_memory (শুধু Target Admin-এর কথা ও তার দেওয়া উত্তর)
```

---

## 📑 সূচিপত্র

1. [এক নজরে ফিচার](#-এক-নজরে-ফিচার)
2. [Architecture](#-architecture)
3. [Project structure](#-project-structure)
4. [ধাপে ধাপে Setup (Beginner friendly)](#-ধাপে-ধাপে-setup-beginner-friendly)
5. [Telegram permissions ও Privacy mode (গুরুত্বপূর্ণ)](#-telegram-permissions-ও-privacy-mode-গুরুত্বপূর্ণ)
6. [Configuration (সব environment variable)](#-configuration-সব-environment-variable)
7. [Commands](#-commands)
8. [Group behaviour: bot কখন উত্তর দেয়](#-group-behaviour-bot-কখন-উত্তর-দেয়)
9. [Render-এ Deploy](#-render-এ-deploy)
10. [UptimeRobot setup](#-uptimerobot-setup)
11. [Testing (কীভাবে নিজে যাচাই করবেন)](#-testing-কীভাবে-নিজে-যাচাই-করবেন)
12. [Troubleshooting](#-troubleshooting)
13. [Privacy ও Data](#-privacy-ও-data)
14. [Setup checklist](#-setup-checklist)
15. [Known limitations](#-known-limitations)

---

## ✨ এক নজরে ফিচার

| Feature | বিবরণ |
|---|---|
| 🧠 **Admin memory** | শুধু `TARGET_ADMIN_ID`-এর message `admin_messages` টেবিলে জমা হয় (topic, keyword, crypto-term, embedding সহ) |
| 🔗 **Q&A memory** | Member প্রশ্ন + Admin-এর উত্তর pair হয়ে `qa_memory`-তে যায় (Telegram reply → সবচেয়ে নির্ভরযোগ্য) |
| 🚫 **Unanswered question** | Admin উত্তর না দিলে সেটি AI memory **হয় না** — শুধু lightweight analytics log থাকে |
| 🔎 **Proper retrieval** | PostgreSQL full-text search + keyword/topic match + semantic (local embedding) + recency weighting + Q&A priority |
| 🎯 **Context control** | `MAX_MEMORY_RESULTS`, `MAX_QA_RESULTS`, `MAX_CONTEXT_CHARS` — প্রতিটি configurable |
| 💸 **Cost control** | Cache (memory + DB), শুধু trigger-এ AI call, bounded prompt, timeout ও retry |
| 🗣️ **Admin style** | Tone sample + style rules — ভাষা, গঠন, বাক্যের ধরন Admin-এর মতো, কিন্তু নকল নয় |
| 🛡️ **Hallucination guard** | System prompt-এ কঠোর নিয়ম: memory ছাড়া Admin-এর নামে কিছু বলা নিষিদ্ধ |
| ⚡ **Error resilience** | Groq/Telegram/DB-র যেকোনো error-এ bot crash করে না, friendly fallback দেয় |
| 🌐 **Flask + Webhook** | `/webhook`, `/health` (UptimeRobot-ready), secret-token protected |
| ☁️ **Render Free ready** | `Procfile`, `render.yaml`, gunicorn start command, 512 MB-friendly |

---

## 🏗 Architecture

```
                          ┌──────────────────────────────────────────┐
   Telegram ──update──▶   │  Flask  (gunicorn, Render free web)      │
                          │  POST /webhook  (X-Telegram-...-Token)   │
                          │  GET  /health   (UptimeRobot)            │
                          └───────────────┬──────────────────────────┘
                                          │ ApplicationManager.process_update()
                                          ▼
                          ┌──────────────────────────────────────────┐
                          │ python-telegram-bot Application           │
                          │ (background thread + asyncio event loop)  │
                          └───────────────┬──────────────────────────┘
                                          ▼
        ┌───────────────────┬─────────────────────────┬────────────────────┐
        │ Permissions       │ MessageTracker          │ AnswerService      │
        │ (trigger + role)  │ (memory + Q&A pairing)  │ (retrieval + Groq) │
        └─────────┬─────────┴───────────┬─────────────┴──────────┬─────────┘
                  │                     │                        │
                  │                     ▼                        ▼
                  │        ┌────────────────────────┐   ┌──────────────────┐
                  │        │ PostgreSQL (Supabase)  │   │ Groq API         │
                  │        │ admin_messages         │   │ GROQ_MODEL env   │
                  │        │ qa_memory              │   └──────────────────┘
                  │        │ pending_questions      │            ▲
                  │        │ message_logs (analytics)│           │
                  │        │ bot_users/bot_settings │   RetrievalEngine
                  │        │ response_cache         │   (score → rank → trim)
                  └────────┴────────────────────────┘
```

**কেন Flask + webhook (polling নয়)?** Render Free-তে background worker চলে না, তাই Telegram update Flask-এ আসে এবং `ApplicationManager` সেটি একটি background thread-এর event loop-এ `Application.process_update()` দিয়ে feed করে (PTB v20+ এর official pattern)। বিস্তারিত: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)।

---

## 📁 Project structure

```
info-group-ai-bot/
├── app.py                      # Flask app: /, /health, /webhook, /set_webhook
├── bot.py                      # PTB Application lifecycle (webhook + polling), webhook setup
├── config.py                   # সব configuration (.env), validation, secret redaction
├── database.py                 # PostgreSQL/Supabase data access (pooled, parameterised SQL)
├── services.py                 # service container + startup/maintenance tasks
├── ai/
│   ├── __init__.py
│   ├── groq_client.py          # Groq API client (retry, timeout, typed errors)
│   ├── embeddings.py           # free local embeddings (no vector DB needed)
│   ├── retrieval.py            # relevance scoring → ranking → context budget
│   ├── prompts.py              # system prompt + style rules + safety rules
│   ├── responder.py            # full answer pipeline + cache + usage logging
│   └── caching.py              # 2-layer answer cache
├── bot_telegram/               # ⚠️ নাম 'telegram' নয় (কারণ নিচে দেখুন)
│   ├── handlers.py             # সব command + message handler + error handler
│   ├── message_tracker.py      # admin memory + Q&A pairing engine
│   └── permissions.py          # role check, trigger detection, group-admin cache
├── utils/
│   ├── logger.py               # logging + automatic secret redaction
│   ├── helpers.py              # caches, rate limiter, chunking, formatting
│   └── text.py                 # Bangla/Banglish NLP: question detect, keywords, topics
├── database/
│   └── schema.sql              # সম্পূর্ণ PostgreSQL schema (idempotent)
├── scripts/                    # setup + maintenance tools (নিচে দেখুন)
├── tests/                      # 16 test module, 290+ assertions (pytest)
├── docs/                       # ARCHITECTURE, DEPLOYMENT, TROUBLESHOOTING, PRIVACY, CHECKLIST
├── requirements.txt            # production dependencies
├── requirements-dev.txt        # + pytest
├── Procfile                    # Render/Heroku start command
├── render.yaml                 # Render Blueprint (optional)
├── runtime.txt                 # python-3.11.9
├── pytest.ini
├── .env.example                # ← এখান থেকে কপি করে .env বানাবেন
├── .gitignore
├── LICENSE
└── README.md
```

> **কেন `bot_telegram/` এবং `telegram/` নয়?**
> PyPI-এর `python-telegram-bot` প্যাকেজটি `import telegram` নামে import হয়। যদি আমাদের নিজের ফোল্ডারের নামও `telegram` হতো, তাহলে সেটি আসল লাইব্রেরিকে shadow করে ফেলত এবং `from telegram.ext import Application` ব্যর্থ হতো। এজন্য নিজের প্যাকেজের নাম `bot_telegram/` রাখা হয়েছে।

---

## 🚀 ধাপে ধাপে Setup (Beginner friendly)

প্রতিটি ধাপ ক্রমানুসারে করলেই হবে। যেখানে value বসাতে হবে সেখানে **exact করে** বলা আছে।

### 1️⃣ Project download / clone

ZIP ফাইলটি extract করুন, অথবা GitHub-এ upload করে clone করুন:

```bash
cd info-group-ai-bot
```

### 2️⃣ Python install (3.11 বা তার উপরে)

```bash
python --version      # 3.11+ হতে হবে (3.12 / 3.13 ও চলবে)
```

যদি না থাকে: [python.org/downloads](https://www.python.org/downloads/) → install করার সময় **"Add Python to PATH"** টিক দিন।

### 3️⃣ Virtual environment তৈরি

**Windows (PowerShell / CMD):**
```bash
python -m venv .venv
.venv\Scripts\activate
```

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 4️⃣ Dependencies install

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

(টেস্ট চালাতে চাইলে: `pip install -r requirements-dev.txt`)

### 5️⃣ Telegram Bot তৈরি (BotFather)

1. Telegram-এ **@BotFather** কে message দিন → `/newbot`
2. একটি নাম দিন (যেমন `INFO GROUP AI BOT`)
3. একটি username দিন যেটি `bot` দিয়ে শেষ হয় (যেমন `InfoGroupAIBot`)
4. BotFather যে token দেবে (যেমন `123456789:AAH...`) সেটি কপি করুন → এটি `.env`-এর **`BOT_TOKEN`**
5. BotFather-এ `/setprivacy` → bot সিলেক্ট → **Disable** (কারণ নিচে ব্যাখ্যা করা আছে — ধাপ ১৩)

### 6️⃣ Groq API key

1. [console.groq.com/keys](https://console.groq.com/keys) → sign up (free)
2. **Create API Key** → কপি করুন (`gsk_...`) → এটি `.env`-এর **`GROQ_API_KEY`**
3. Model পরিবর্তন করতে চাইলে শুধু `.env`-এ বদলান:
   ```env
   GROQ_MODEL=openai/gpt-oss-120b     # default (recommended)
   # GROQ_MODEL=openai/gpt-oss-20b   # সস্তা/দ্রুত বিকল্প
   # GROQ_MODEL=llama-3.3-70b-versatile
   ```
   কোনো model নাম code-এ hard-code করা নেই।
4. Key-টি Groq dashboard-এর model list-এর সাথে মिलছে কি না দেখতে:
   ```bash
   python scripts/check_config.py --online
   ```

### 7️⃣ Supabase project + database

1. [supabase.com](https://supabase.com) → **New project** (free)। Region এমনভাবে বাছুন যেটি আপনার/আপনার গ্রুপের কাছাকাছি (যেমন Singapore)
2. Project তৈরি হওয়ার পর: **Project Settings → Database → Connection string → URI**
3. Password দেখতে **Reset database password** চাপুন (নতুন password কপি করে রাখুন)
4. URI-টি কপি করুন। Free tier-এ **Connection pooling → Session** ব্যবহার করুন:
   ```
   postgresql://postgres.abcdefghijklm:[YOUR-PASSWORD]@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres
   ```
   এটি `.env`-এর **`DATABASE_URL`**
   > `?pgbouncer=true` এর মতো extra parameter থাকলে চিন্তা করবেন না — bot সেগুলো নিজে পরিষ্কার করে নেয়।

### 8️⃣ SQL schema চালান (একবার)

**Option A — Supabase Dashboard (সহজ):**
1. Supabase → **SQL Editor** → **New query**
2. এই project-এর `database/schema.sql` ফাইলের সম্পূর্ণ content paste করুন
3. **Run** → "Success" দেখতে পাবেন
   (এটি ইচ্ছাকৃতভাবে idempotent — দুবার চালালেও সমস্যা হবে না)

**Option B — terminal:**
```bash
python scripts/apply_schema.py            # DATABASE_URL ব্যবহার করে
python scripts/apply_schema.py --dry-run  # শুধু দেখতে চাইলে
```

### 9️⃣ `.env` configure

```bash
cp .env.example .env          # Windows: copy .env.example .env
```

তারপর `.env` ফাইল খুলে **অবশ্যই** এই value গুলো বসান:

| Variable | কোথা থেকে পাবেন | উদাহরণ |
|---|---|---|
| `BOT_TOKEN` | ধাপ 5️⃣ (BotFather) | `123456789:AAH...` |
| `GROQ_API_KEY` | ধাপ 6️⃣ (Groq console) | `gsk_...` |
| `GROQ_MODEL` | default রাখলেই চলবে | `openai/gpt-oss-120b` |
| `DATABASE_URL` | ধাপ 7️⃣ (Supabase URI) | `postgresql://postgres.xxx:pw@....pooler.supabase.com:5432/postgres` |
| `ADMIN_ID` | আপনার নিজের Telegram id (ধাপ ১৩) | `123456789` |
| `TARGET_ADMIN_ID` | যে Admin-এর কথা memory হবে (সাধারণত একই) | `123456789` |
| `GROUP_ID` | গ্রুপের id (ধাপ ১৩) | `-1001234567890` |
| `PUBLIC_URL` | শুধু deploy-এর পরে (Render URL) | `https://info-group-ai-bot.onrender.com` |
| `WEBHOOK_SECRET` | নিচের command দিয়ে বানান | `x9f...` |

Secret তৈরি করুন:
```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

> 🔐 `.env` কখনো GitHub-এ push করবেন না — `.gitignore`-তে আগেই রাখা আছে।

### 🔟 Local test run (দুইভাবে)

**A) Polling (সবচেয়ে সহজ, webhook/PUBLIC_URL লাগে না):**
```bash
python bot.py
```
Telegram-এ bot-কে message দিলেই উত্তর আসবে। বন্ধ করতে `Ctrl + C`.

**B) Flask + webhook (production-এর মতো টেস্ট):**
```bash
python app.py            # http://localhost:5000
curl http://localhost:5000/health
```
Local webhook টেস্ট করতে ngrok/cloudflared দরকার — [নিচে দেখুন](#-লোকাল-webhook-টেস্ট-ngrok--cloudflared)।

### 1️⃣1️⃣ Telegram group-এ bot যোগ করুন

1. BotFather এ banano bot-কে group-এ **Add member** করুন (Admin হতে হবে না, তবে group-এ add করা বাধ্যতামূলক)
2. Group Settings → **Administrators**-এ bot-কে যোগ করলে Telegram কয়েকটি কাজ সহজ করে দেয় (message delete হলে error কম হয়) — তবে এটা বাধ্যতামূলক নয়
3. Group-এ লিখুন: `/whoami@আপনার_bot_username` → এটি আপনার **user id** ও **chat id** দেখাবে
4. সেই 값을 `.env`-এর `ADMIN_ID`, `TARGET_ADMIN_ID`, `GROUP_ID`-তে বসান এবং bot restart করুন

### 1️⃣2️⃣ Bot permissions ও Privacy mode

Telegram-এ bot দিয়ে group-এর **সব** message পড়তে চাইলে privacy mode **Disable** করতে হয়:

**@BotFather → `/setprivacy` → আপনার bot সিলেক্ট → `Disable`**

> ⚠️ Privacy mode **Enabled** থাকলে bot শুধু এমন message দেখে যেগুলো তাকে mention/reply করা হয়েছে বা command। অর্থাৎ Admin-এর স্বাভাবিক কথা memory-তে জমা হতো না।
> Privacy পরিবর্তনের পর bot-কে group থেকে **একবার remove করে আবার add** করুন (Telegram তখনই নতুন সেটিং প্রয়োগ করে)।

Bot-এর যা লাগে (এবং যা লাগে না):

| Permission | দরকার? | কারণ |
|---|---|---|
| Group message পড়া (privacy off) | ✅ **হ্যাঁ** | Admin memory + Q&A detection |
| Send messages | ✅ হ্যাঁ | উত্তর দেওয়া, command reply |
| Delete messages | ❌ না | Bot কখনো delete করে না |
| Ban users | ❌ না | কখনো ব্যবহার হয় না |
| Read chat history (আগের message) | ❌ না | শুধু Bot যোগ হওয়ার পরে যেগুলো আসে সেগুলোই পড়ে |

### 1️⃣3️⃣ Target Admin ID বের করা

1. Bot-কে private-এ `/whoami` পাঠান → `user id` = আপনার Telegram id
2. Group-এ `/whoami` পাঠান → `chat id` = গ্রুপের id (negative number, যেমন `-1001234567890`)
3. `.env`-এ বসান:
   ```env
   ADMIN_ID=123456789
   TARGET_ADMIN_ID=123456789
   GROUP_ID=-1001234567890
   ```
4. `python scripts/check_config.py` চালিয়ে নিশ্চিত হোন ✅

### 1️⃣4️⃣ লোকাল webhook টেস্ট (ngrok / cloudflared)

```bash
# terminal 1
python app.py                     # port 5000

# terminal 2
ngrok http 5000                   # অথবা: cloudflared tunnel --url http://localhost:5000
```
ngrok যে `https://xxxx.ngrok-free.app` URL দেবে সেটি `.env`-এর `PUBLIC_URL`-এ বসান, তারপর:
```bash
python scripts/set_webhook.py --set
python scripts/set_webhook.py --info      # যাচাই
```
Telegram-এ bot-কে `/ping` পাঠান — উত্তর এলেই webhook ঠিক আছে ✅

### 1️⃣5️⃣ GitHub-এ upload

```bash
git init
git add .
git commit -m "INFO GROUP AI BOT v1.0.0"
git branch -M main
git remote add origin https://github.com/USERNAME/info-group-ai-bot.git
git push -u origin main
```

Push করার আগে শেষবার যাচাই করুন:
```bash
git status --short          # .env যেন কোথাও না থাকে
python scripts/check_config.py
```
`.gitignore`-তে `.env` আছে, তাই ভুল করে commit হবে না।

### 1️⃣6️⃣ Render-এ Deploy

👉 বিস্তারিত ধাপ: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** (সংক্ষেপে নিচেও দেওয়া আছে)

### 1️⃣7️⃣ Environment variables (Render-এ)

Render Dashboard → আপনার service → **Environment** → নিচের key গুলো value সহ যোগ করুন:
`BOT_TOKEN`, `GROQ_API_KEY`, `DATABASE_URL`, `ADMIN_ID`, `TARGET_ADMIN_ID`, `GROUP_ID`, `PUBLIC_URL` (`https://আপনার-service.onrender.com`), `WEBHOOK_SECRET`, `GROQ_MODEL` (optional)

### 1️⃣8️⃣ UptimeRobot setup

👉 নিচে **[UptimeRobot setup](#-uptimerobot-setup)** দেখুন — এটিই Render-এর free service-কে ঘুমিয়ে পড়া থেকে বাঁচায়।

### 1️⃣9️⃣ প্রথম টেস্ট

1. Group-এ Admin (TARGET_ADMIN_ID) হিসেবে লিখুন: `BTC এখন entry না নেওয়াই ভালো, confirmation আসুক`
2. অন্য একটি account/বন্ধু দিয়ে প্রশ্ন করুন: `/ask BTC এখন entry নেওয়া যাবে?`
3. Bot Admin-এর কথা memory থেকে উত্তর দেবে ✅
4. Admin-এখোন `TARGET_ADMIN` হিসেবে সেই প্রশ্নে **reply** দিয়ে বলুন: `এখন না, ধৈর্য ধরো` → এটি `qa_memory`-তে pair হয়ে যাবে
5. `/memory` পাঠিয়ে যাচাই করুন

---

## ⚙️ Configuration (সব environment variable)

সম্পূর্ণ তালিকা ও default `.env.example` ফাইলে (comment সহ) আছে। গুরুত্বপূর্ণগুলো:

| Variable | Default | কাজ |
|---|---|---|
| `BOT_TOKEN` | — | **required** Telegram bot token |
| `GROQ_API_KEY` | — | **required** Groq key (না থাকলে bot চলবে, কিন্তু AI উত্তর দেবে না) |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | যেকোনো Groq chat model |
| `DATABASE_URL` | — | **required** Supabase/PostgreSQL URI |
| `TARGET_ADMIN_ID` | — | কার message memory হবে (comma-separated একাধিকও চলে) |
| `ADMIN_ID` | — | কে admin command চালাতে পারবে |
| `GROUP_ID` | — | নির্দিষ্ট গ্রুপ (খালি রাখলে যেকোনো গ্রুপ) |
| `PUBLIC_URL` | — | Render URL (webhook auto-setup-এর জন্য) |
| `WEBHOOK_SECRET` | — | webhook protect করার secret |
| `MAX_MEMORY_RESULTS` | `10` | সর্বোচ্চ কতটি admin memory prompt-এ যাবে |
| `MAX_QA_RESULTS` | `5` | সর্বোচ্চ কতটি Q&A pair prompt-এ যাবে |
| `MAX_CONTEXT_CHARS` | `12000` | context-এর সর্বোচ্চ অক্ষর (cost control) |
| `QA_PAIR_WINDOW_SECONDS` | `300` | reply না দিলে কত সেকেন্ডের মধ্যে pairing হবে |
| `QA_PAIR_REQUIRE_QUESTION` | `false` | true হলে শুধু প্রশ্ন-সদৃশ message pair হবে |
| `RATE_LIMIT_PER_MINUTE` | `6` | প্রতি member প্রতি মিনিটে সর্বোচ্চ AI উত্তর |
| `CACHE_ENABLED` | `true` | একই প্রশ্নে আবার AI call এড়ানো |
| `EMBEDDING_ENABLED` | `true` | local semantic search (কোনো paid service লাগে না) |
| `AI_DISCLOSURE_TEXT` | `🤖 AI উত্তর (...)` | গ্রুপে উত্তরের নিচের disclosure line |
| `DB_POOL_MAX` | `4` | Supabase connection pool size |
| `GROQ_API_URL` | `https://api.groq.com/openai/v1/chat/completions` | Groq-compatible endpoint (বদলানোর দরকার নেই) |
| `GROQ_MODELS_URL` | `https://api.groq.com/openai/v1/models` | `--online` চেক ও model verification |
| `ADMIN_IDS` | — | `ADMIN_ID` ছাড়াও অতিরিক্ত admin (comma-separated) |
| `AUTOSTART_BOT` | `true` | `false` করলে bot background-এ connect করবে না (শুধু offline tooling/tests) |
| `LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR |

সম্পূর্ণ যাচাই:
```bash
python scripts/check_config.py            # offline
python scripts/check_config.py --online   # Groq + Telegram API সহ
```

### 🛠 Helper scripts (সব `scripts/` ফোল্ডারে)

| Command | কাজ |
|---|---|
| `python scripts/apply_schema.py` | `database/schema.sql` চালানো (idempotent — বারবার চালানো নিরাপদ) |
| `python scripts/check_config.py [--online]` | `.env`, DB, Groq, Telegram সব যাচাই |
| `python scripts/set_webhook.py --set` | Telegram-এ webhook সেট/আপডেট (`--info`, `--delete` ও আছে) |
| `python scripts/set_commands.py` | `/start`, `/help` ... কমান্ড মেনু Telegram-এ রেজিস্টার |
| `python scripts/simulate_conversation.py [-v]` | ইন্টারনেট ছাড়াই পুরো pipeline ডেমো (DB/TG লাগে না) |
| `python scripts/seed_demo_memory.py --clear-first` | টেস্টের জন্য demo memory ঢোকায় ⚠️ শুধু টেস্ট ডেটাবেজে |
| `python scripts/export_memory.py --format md --out backup.md` | memory ব্যাকআপ (json/csv/md) |
| `python scripts/maintenance.py [--dry-run] [--vacuum]` | পুরোনো log/cache পরিষ্কার (bot নিজেও করে) |

---

## 💬 Commands

**সবার জন্য:**

| Command | কাজ |
|---|---|
| `/ask <প্রশ্ন>` | AI উত্তর (`/ask BTC এখন entry নেওয়া যাবে?`) |
| `/start` | পরিচিতি ও ব্যবহারবিধি |
| `/help` | সাহায্য |
| `/memory` | আমি কী মনে রেখেছি (সারসংক্ষেপ) |
| `/memory <প্রশ্ন>` | ওই প্রশ্নের সাথে কী কী memory match করেছে (score সহ) |
| `/status` | bot + AI + database status |
| `/whoami` | আপনার Telegram id ও chat id (setup-এর জন্য) |
| `/ping` | bot সাড়া দিচ্ছে কি না |

**শুধু Admin (ADMIN_ID):**

| Command | কাজ |
|---|---|
| `/stats` | admin messages, Q&A, users, unanswered, AI calls/tokens, DB latency |
| `/memory_stats` | topic breakdown, latest memory, unanswered প্রশ্নের তালিকা |
| `/clear_memory <scope> confirm` | memory মুছে ফেলা — `scope`: `qa` / `admin` / `logs` / `cache` / `all` |
| `/set <key> <on\|off>` | `ai_enabled`, `auto_reply_enabled`, `memory_enabled`, `qa_pairing_enabled`, `reply_footer_enabled` |
| `/reload` | `.env` আবার পড়ে settings refresh |
| `/maintenance` | expire/purge housekeeping এখনই চালানো |
| `/adminhelp` | admin command-এর তালিকা ও switch-এর অবস্থা |

`/clear_memory` **সবসময় confirmation চায়** — শুধু `/clear_memory all` পাঠালে কী কী মুছে যাবে তা দেখিয়ে confirm-এর instruction দেয়।

---

## 🗣 Group behaviour: bot কখন উত্তর দেয়

Bot ইচ্ছাকৃতভাবে **চুপচাপ** থাকে; সব message-এ reply করে না। উত্তর দেয় শুধু:

* `/ask <প্রশ্ন>` command-এ
* কেউ bot-কে **mention** করলে — `@InfoGroupAIBot BTC কেমন?`
* কেউ bot-এর message-এ **reply** দিলে
* `TRIGGER_KEYWORDS`-এ থাকা শব্দ থাকলে (যেমন `TRIGGER_KEYWORDS=askai`)

বাকি সব message **নীরবে track** হয়:
* Target Admin-এর message → `admin_messages` (memory)
* Member-এর message → প্রশ্ন হলে `pending_questions`, এবং unanswer করা হলে analytics log
* Admin reply করলে → `qa_memory` (জোড়া)

`/set auto_reply_enabled off` দিয়ে চাইলে গ্রুপে অটো-উত্তর বন্ধ রাখা যায় (memory জমা হতে থাকবে)।

---

## ☁️ Render-এ Deploy

**সংক্ষিপ্ত ধাপ (বিস্তারিত: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)):**

1. GitHub-এ project push করা আছে কি না নিশ্চিত হোন
2. [render.com](https://render.com) → **New +** → **Web Service** → repo সিলেক্ট করুন
3. সেটিংস:
   * **Name:** `info-group-ai-bot`
   * **Region:** Singapore (Bangladesh-এর জন্য সবচেয়ে কাছের free region)
   * **Runtime:** Python 3
   * **Build Command:** `pip install --upgrade pip && pip install -r requirements.txt`
   * **Start Command:** `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --access-logfile - --error-logfile - app:app`
   * **Instance Type:** Free
4. **Environment** → `.env`-এর সব key যোগ করুন, তবে `PUBLIC_URL` হবে আপনার Render URL: `https://info-group-ai-bot.onrender.com`
5. **Create Web Service** → deploy শেষ হলে ব্রাউজারে খুলুন: `https://আপনার-service.onrender.com/health`
   ```json
   {"status":"ok","service":"INFO GROUP AI BOT", "...": "..."}
   ```
6. Webhook রেজিস্টার হবে প্রথম request/startup-এ। যাচাই:
   ```
   https://আপনার-service.onrender.com/set_webhook?token=<WEBHOOK_SECRET>
   ```
   অথবা লোকালি: `python scripts/set_webhook.py --info`

> **কেন `--workers 1`?** প্রক্রিয়া প্রতি একটিই PTB Application চলে (background thread)। একাধিক worker দিলে প্রতিটি আলাদা করে init করবে — Telegram webhook-এ ঠিক আছে, কিন্তু rate-limit counters আলাদা হয়ে যাবে এবং duplicate processing-এর আশঙ্কা থাকে। 1 worker + 4 thread free tier-এর জন্য যথেষ্ট।

**Start command পরিবর্তন কখন করবেন:** যদি আপনি PTB-র নিজের polling চান (Render worker নয়, web service-এ), `Start Command` = `python bot.py` দিন এবং `PUBLIC_URL` ফাঁকা রাখুন। Free tier-এ এটি **সুপারিশ করা হয় না** (service ঘুমিয়ে পড়লে polling বন্ধ হয়ে যায়)।

---

## ⏰ UptimeRobot setup

Render free web service ১৫ মিনিট নিষ্ক্রিয় থাকলে sleep করে। ৫ মিনিটের health ping এটিকে জাগিয়ে রাখে।

1. [uptimerobot.com](https://uptimerobot.com) → free account
2. **Add New Monitor**:
   * **Monitor Type:** `HTTP(s)`
   * **Friendly Name:** `INFO GROUP AI BOT`
   * **URL:** `https://আপনার-service.onrender.com/health`
   * **Monitoring Interval:** `5 minutes`
   * **Alert Contacts:** আপনার email
3. **Create Monitor** → কয়েক মিনিটেই status **Up** দেখাবে

`/health` response (যা UptimeRobot যাচাই করে):
```json
{
  "status": "ok",
  "service": "INFO GROUP AI BOT",
  "version": "1.0.0",
  "uptime_seconds": 4231,
  "ai": {"enabled": true, "model": "openai/gpt-oss-120b"},
  "database": {"available": true, "latency_ms": 34},
  "telegram": {"mode": "webhook", "started": true, "updates_received": 128}
}
```

---

## 🧪 Testing (কীভাবে নিজে যাচাই করবেন)

```bash
pip install -r requirements-dev.txt
pytest -q                     # সব test
pytest -q -v                  # বিস্তারিত
pytest -q tests/test_integration_flow.py    # শুধু end-to-end
```

Database-নির্ভর test গুলো একটি PostgreSQL দরকার হয়। Railway/Supabase-এর একটি আলাদা (throwaway) database দিন:

```bash
export TEST_DATABASE_URL="postgresql://[USER]:[PASSWORD]@[HOST]:5432/[TEST_DB_NAME]"
pytest -q
```

> ⚠️ উপরের `TEST_DATABASE_URL` শুধু **test** ডেটাবেজের জন্য — এখানে আলাদা খালি ডেটাবেজ দিন, কারণ টেস্ট শেষে টেবিলগুলো truncate হয়।
> নিজের production DATABASE_URL এখানে দেবেন না।
Database না থাকলে সেগুলো **skip** হবে (fail নয়) — মেসেজে দেখাবে কীভাবে চালাতে হবে।

**আরও যাচাই:**

```bash
python scripts/check_config.py --online      # env + DB + Groq + Telegram
python scripts/simulate_conversation.py -v   # Telegram ছাড়া পুরো pipeline demo
python scripts/apply_schema.py               # schema (idempotent)
python scripts/maintenance.py --vacuum       # housekeeping
python scripts/export_memory.py --format json --out memory_backup.json  # backup
python scripts/seed_demo_memory.py --dry-run # demo data দেখুন, তারপর --clear-first দিয়ে ঢোকান
```

Test module গুলো:

| File | কী যাচাই করে |
|---|---|
| `test_config.py` | env parsing, validation, URL normalisation, secret redaction |
| `test_text_utils.py` | Bangla/Banglish question detection, keyword, topic, similarity |
| `test_helpers_logging.py` | caches, rate limiter, chunking, **log redaction** |
| `test_permissions.py` | role check, mention/reply/keyword trigger |
| `test_message_tracker.py` | admin memory, reply/window pairing, false-pairing বন্ধ, unanswered question |
| `test_retrieval.py` | relevance scoring, limits, dedupe, context budget, embeddings |
| `test_responder.py` | পুরো answer pipeline, cache, error fallback, safety post-processing |
| `test_groq_client.py` | Groq error mapping (timeout/429/401/404), retry, payload |
| `test_flask_app.py` | `/health`, `/webhook` security, malformed update, set_webhook |
| `test_handlers.py` | প্রতিটি command, admin permission, confirmation, answer flow |
| `test_database.py` | schema, tables, SQL injection safety, stats, clear scopes |
| `test_integration_flow.py` | সম্পূর্ণ lifecycle + cache + usage log |
| `test_imports_and_structure.py` | module/import/table/env নামের cross-check |
| `test_security_audit.py` | hardcoded secret নেই, SQL parameterised, webhook protected |

---

## 🔧 Troubleshooting

| সমস্যা | কারণ ও সমাধান |
|---|---|
| `/health` এ `"status": "degraded"` | `problems` field দেখুন — সাধারণত `BOT_TOKEN`/`DATABASE_URL` খালি |
| Bot কোনো উত্তর দেয় না | Group-এ bot-এর privacy mode **Disable** করা হয়েছে কি? Add করার পর remove→re-add করেছেন? `/ping` কাজ করে কি? |
| `database unavailable` | Supabase URI ভুল/password বদলেছে, অথবা IP restriction। `python scripts/check_config.py` চালান |
| `missing tables` | `database/schema.sql` চালানো হয়নি → Supabase SQL Editor-এ Run করুন |
| Admin-এর কথা memory-তে যাচ্ছে না | `TARGET_ADMIN_ID` কি আসল Admin-এর id? `/whoami` দিয়ে মিলিয়ে দেখুন। Privacy mode? |
| Q&A pair হচ্ছে না | Admin সেটি **reply** করে উত্তর দিন (সবচেয়ে নির্ভরযোগ্য)। অথবা `QA_PAIR_WINDOW_SECONDS` বাড়ান |
| `invalid connection option "pgbouncer"` | পুরোনো version; বর্তমান code নিজেই parameter পরিষ্কার করে। `pip install -r requirements.txt` চালিয়ে update করুন |
| Groq `rate_limited` | Free tier limit শেষ — কিছুক্ষণ অপেক্ষা; cache চালু রাখলে duplicate প্রশ্ন এড়ানো যায় |
| Groq `model_unavailable` | `.env`-এ `GROQ_MODEL` বদলান (Groq-এর current model list দেখুন) |
| উত্তর বাংলায় আসছে না | প্রশ্নের ভাষা অনুসরণ করার নিয়ম prompt-এ আছে; `AI_DISCLOSURE_TEXT`/`GROQ_TEMPERATURE` ঠিক আছে কি? |
| Render deploy ব্যর্থ | **Logs** ট্যাব দেখুন; সাধারণত `DATABASE_URL`-এ password-এ special character (`@`, `:`) — সেগুলো URL-encode করতে হয় |
| Service ঘুমিয়ে পড়ে | UptimeRobot monitor যোগ করুন (উপরে) |
| Update duplicate / একই উত্তর দুইবার | `DROP_PENDING_UPDATES=true` রাখুন; একই update_id আবার এলে dedupe বাদ দেয় |

আরও: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)

---

## 🔐 Privacy ও Data

* **কী জমা হয়:** Target Admin-এর message (`admin_messages`), Admin-এর দেওয়া উত্তর সহ Q&A pair (`qa_memory`), member প্রশ্ন-উত্তর pairing-এর জন্য অস্থায়ী `pending_questions`, এবং lightweight `message_logs` (analytics, কাটছাঁট করা, AI-তে **যায় না**)।
* **কী জমা হয় না:** যেসব প্রশ্নের উত্তর Admin দেননি সেগুলো AI memory হয় না। Bot অন্য bot/অ্যাডমিনদের কথা memory বানায় না।
* **AI-তে কী যায়:** পুরো database নয় — শুধু retrieval-এ নির্বাচিত সর্বোচ্চ `MAX_MEMORY_RESULTS` + `MAX_QA_RESULTS` আইটেম, সর্বোচ্চ `MAX_CONTEXT_CHARS` অক্ষরে।
* **Secrets:** `.env`, `.gitignore`-এ আছে; log-এ কোনো token/key/database password কখনো যায় না (automatic redaction)।
* **SQL:** সব query parameterised — SQL injection প্রতিরোধ সহ।
* **মুছে ফেলা:** `/clear_memory <scope> confirm` — `qa`, `admin`, `logs`, `cache`, `all`।
* **Group-কে জানানো:** bot-এর প্রতিটি AI উত্তরের নিচে disclosure থাকে (`AI_DISCLOSURE_TEXT`)। গ্রুপের member-দের জানানো উচিত যে memory তৈরি হচ্ছে।

বিস্তারিত: [docs/PRIVACY_AND_SECURITY.md](docs/PRIVACY_AND_SECURITY.md)

---

## ✅ Setup checklist

```
[ ] Telegram bot created (BotFather) ও token কপি করা হয়েছে
[ ] BOT_TOKEN .env / Render-এ যোগ করা হয়েছে
[ ] TARGET_ADMIN_ID যোগ করা হয়েছে
[ ] GROUP_ID যোগ করা হয়েছে
[ ] ADMIN_ID যোগ করা হয়েছে
[ ] Groq API key নেওয়া ও GROQ_API_KEY সেট করা হয়েছে
[ ] GROQ_MODEL ঠিক আছে (openai/gpt-oss-120b)
[ ] Supabase project তৈরি, DATABASE_URL সেট করা হয়েছে
[ ] database/schema.sql চালানো হয়েছে (missing tables নেই)
[ ] Bot group-এ add করা হয়েছে
[ ] Group permissions ঠিক আছে (message read + send)
[ ] Privacy mode Disable করা হয়েছে (@BotFather → /setprivacy)
[ ] Render-এ deploy হয়েছে (Build/Start command ঠিক)
[ ] PUBLIC_URL = Render URL সেট করা হয়েছে
[ ] WEBHOOK_SECRET তৈরি ও সেট করা হয়েছে
[ ] https://আপনার-service.onrender.com/health → "status": "ok"
[ ] Telegram webhook configured (scripts/set_webhook.py --info)
[ ] UptimeRobot monitor (5 min) চালু
[ ] /whoami দিয়ে id যাচাই করা হয়েছে
[ ] /ask দিয়ে একটি টেস্ট প্রশ্ন সফল হয়েছে
[ ] Admin-এর একটি reply দিয়ে Q&A memory তৈরি যাচাই করা হয়েছে (/memory)
[ ] /stats ও /clear_memory শুধু admin-এর কাছে কাজ করছে
```

---

## ⚠️ Known limitations

সৎভাবে যেগুলো এখনো একটি limitation:

1. **Embedding neural নয়।** Semantic search-টি local hashing + concept vocabulary (dictionary) ভিত্তিক — Render free-এর 512 MB RAM-এ neural model চালানো বাস্তবসম্মত নয়। শব্দার্থিক মিল ভালো, তবে true neural embedding-এর মতো নিখুঁত নয়। Architecture vector-এর জন্য প্রস্তুত (তালিকা: `real[]` কলাম + `EMBEDDING_ENABLED`), তাই পরে একটি hosted embedding provider যোগ করলে schema বদলাতে হবে না।
2. **Bot শুধু live message দেখে।** Telegram history পড়ার API নেই — তাই bot group-এ যোগ হওয়ার **পরে** যেসব message আসে সেগুলোই memory হয় (কোনো historical backfill নেই; চাইলে future version-এ Telegram Desktop export import করা যাবে)।
3. **Multi-worker নয়।** `--workers 1` নির্ধারিত (উपরে কারণ দেখুন)। একাধিক worker ব্যবহার করলে duplicate memory ও rate-limit counter আলাদা হতে পারে।
4. **Bangla stemming সীমিত।** PostgreSQL-এ Bangla stemmer নেই, তাই full-text search `simple` configuration ব্যবহার করে (token intact রাখে) — এটিই Bangla-র জন্য সবচেয়ে ভালো behavior, তবে morphology-aware search নয়।
5. **Realtime AI নয়।** Crypto দাম/মার্কেট ডেটা bot আনে না; সে শুধু Admin-এর শেয়ার করা তথ্য ও তার স্টাইল ব্যবহার করে। "এখন BTC কত?" এমন প্রশ্নে bot Admin-এর আগের opinion দেবে, লাইভ দাম নয় — এবং সেটাই বোঝাবে।
6. **Groq free tier limit** প্রযোজ্য (rate limit হলে bot friendly fallback দেয় ও memory থেকে সংক্ষিপ্ত তথ্য দেয়)।
7. **LLM দিয়ে করা style mimicry নির্ভুল নয়** — ৯৫% নয়, তবে একই গ্রুপে Admin-এর মতো টোন/গঠন আসে। Prompt tuning → `ai/prompts.py`।

---

## 📄 License

MIT — [LICENSE](LICENSE) দেখুন।

## 🙏 ধন্যবাদ

সব configuration `.env`-এ, আর bot-এর behaviour `ai/prompts.py` ও `bot_telegram/message_tracker.py`-এ কেন্দ্রীভূত — তাই কোনো পছন্দ বদলাতে হলে শুধু ঐ জায়গাগুলো edit করলেই হবে।

# 🚀 ZIP → GitHub → Render : A to Z ডিপ্লয় গাইড

> **কে এই গাইডের জন্য:** যিনি আগে কখনো bot deploy করেননি। প্রতিটি ক্লিক, প্রতিটি কমান্ড হুবহু দেওয়া আছে।
> মোট সময়: **~৪০–৫০ মিনিট** (কফি নিয়ে বসুন ☕) · খরচ: **সম্পূর্ণ ফ্রি**

---

## 📋 সূচিপত্র

| ধাপ | কী করবেন | সময় |
|---|---|---|
| [০](#০-শুরুর-আগে-কী-কী-লাগবে) | শুরুর আগে: ৬টি অ্যাকাউন্ট | ১০ মি |
| [১](#১-zip-extract-করুন) | ZIP extract | ১ মি |
| [২](#২-কোন-ফাইল-github-এ-যাবে--কোনটা-যাবে-না) | **কোন ফাইল upload করবেন, কোনটা না** | ৫ মি |
| [৩](#৩-botfather--bot-token-নিন) | BotFather → BOT_TOKEN | ৩ মি |
| [৪](#৪-groq-api-key-নিন) | Groq → GROQ_API_KEY | ৩ মি |
| [৫](#৫-supabase-project--database-url) | Supabase → DATABASE_URL | ৮ মি |
| [৬](#৬-schema-চালান-টেবিল-তৈরি) | schema.sql চালানো | ২ মি |
| [৭](#৭-github-এ-code-upload) | GitHub-এ push | ৭ মি |
| [৮](#৮-render-এ-deploy) | Render deploy | ১০ মি |
| [৯](#৯-webhook-সেট-করা) | Webhook সেট | ২ মি |
| [১০](#১০-grup-এ-bot-যোগ-করা--টেস্ট) | গ্রুপ setup + টেস্ট | ৫ মি |
| [১১](#১১-uptimerobot-ঘুম-থেকে-বাঁচান) | UptimeRobot | ৩ মি |
| [১২](#১২-সব-ঠিক-হয়েছে-কি-না-চূড়ান্ত-চেকলিস্ট) | চূড়ান্ত চেকলিস্ট | ২ মি |
| [১৩](#১৩-কিছু-ভুল-হলে--দ্রুত-সমাধান) | সমস্যা হলে | — |
| [১৪](#১৪-পরে-আপডেট-করতে-চাইলে) | পরে আপডেট | — |

---

## ০. শুরুর আগে: কী কী লাগবে

৬টি ফ্রি অ্যাকাউন্ট (সব ফ্রি, কার্ড লাগে না):

| # | সাইট | কাজ |
|---|---|---|
| ১ | [t.me/BotFather](https://t.me/BotFather) | Telegram bot বানানো + token |
| ২ | [console.groq.com](https://console.groq.com) | AI-এর key |
| ৩ | [supabase.com](https://supabase.com) | ডেটাবেজ (মেমোরি এখানে থাকবে) |
| ৪ | [github.com](https://github.com) | কোড রাখার জায়গা |
| ৫ | [render.com](https://render.com) | bot ২৪/৭ চালানো |
| ৬ | [uptimerobot.com](https://uptimerobot.com) | bot-কে ঘুমাতে না দেওয়া |

আর দরকার: **Python 3.11+** (লোকাল টেস্টের জন্য; [python.org](https://www.python.org/downloads/)) এবং **Git** ([git-scm.com](https://git-scm.com/downloads)) — Git না থাকলে ধাপ ৭-এ বিকল্প দেওয়া আছে।

💡 **সবচেয়ে জরুরি ভুল এড়ান:** Render হলো **IPv4-only** নেটওয়ার্ক, আর Supabase-এর "Direct connection" এখন **IPv6-only**। তাই Supabase থেকে অবশ্যই **Session pooler** লিংক নিতে হবে — নাহলে `Network is unreachable` error আসবে। (ধাপ ৫-এ বিস্তারিত)

---

## ১. ZIP extract করুন

1. `info-group-ai-bot.zip` ফাইলটি ডাউনলোড করুন
2. ডান-ক্লিক → **Extract All** / **Extract Here**
3. ভেতরে `info-group-ai-bot` ফোল্ডার পাবেন — এটাই আপনার পুরো প্রজেক্ট

```
info-group-ai-bot/
├── app.py  bot.py  config.py  database.py  services.py
├── ai/  bot_telegram/  utils/  database/  scripts/  tests/  docs/
└── requirements.txt  Procfile  .python-version  .env.example  ...
```

> ℹ️ **ZIP-এ `.env` ফাইল নেই** — কারণ এতে আপনার গোপন key/token বসে, আর সেটি কখনো Git-এ যাওয়া উচিত নয়। আপনি `.env.example` থেকে নিজের `.env` বানাবেন (লোকাল টেস্টের জন্য, ধাপ ৯-এর শেষে)।

---

## ২. কোন ফাইল GitHub-এ যাবে — কোনটা যাবে না

প্রশ্ন: **"সব ফাইল যাবে?"** — হ্যাঁ, প্রায় সব, আর `.gitignore` ফাইলটি নিজে থেকেই যেগুলো যাওয়া উচিত নয় সেগুলো আটকে দেবে। নিচে পুরো তালিকা:

### ✅ A. অবশ্যই upload হবে (এগুলো ছাড়া bot চলবেই না)

| ফাইল / ফোল্ডার | কেন দরকার |
|---|---|
| `app.py` | Flask server: `/health`, `/webhook` — Render এটাই চালায় |
| `bot.py` | Telegram bot চালু করা, webhook ব্যবস্থাপনা |
| `config.py` | সব `.env` variable পড়া ও যাচাই |
| `database.py` | ডেটাবেজের সব কাজ (memory, pairing, stats) |
| `services.py` | সব অংশ জোড়া লাগানো |
| `ai/` (৮ ফাইল) | Groq client, retrieval, prompt, cache, embedding |
| `bot_telegram/` (৪ ফাইল) | কমান্ড হ্যান্ডলার, admin memory tracker, permission |
| `utils/` (৪ ফাইল) | logging, Bangla/Banglish text processing, helpers |
| `database/schema.sql` | টেবিল তৈরির SQL (Supabase-এ চালাবেন) |
| `requirements.txt` | Render কোন প্যাকেজ ইনস্টল করবে |
| `Procfile` | Render শুরুর কমান্ড |
| `.python-version` | ⚠️ **Render Python version এখান থেকে পড়ে** (`3.11`) — না থাকলে Render 3.14 দেয়, তখন `psycopg2` build fail করতে পারে |
| `.gitignore` | `.env`, cache — এসব যেন কখনো upload না হয় |
| `README.md` | সম্পূর্ণ ডকুমেন্টেশন |
| `LICENSE` | MIT লাইসেন্স |
| `.env.example` | কোন কোন variable লাগবে তার নমুনা (secret নেই) |

### ✅ B. upload করা ভালো (bot চলার জন্য দরকার নেই, কিন্তু ক্ষতিও নেই)

| ফাইল / ফোল্ডার | কী |
|---|---|
| `render.yaml` | Blueprint দিয়ে এক-ক্লিকে deploy করতে চাইলে দরকার |
| `docs/` (৭ ফাইল) | ARCHITECTURE, DEPLOYMENT, TROUBLESHOOTING, PRIVACY, CHECKLIST, এই গাইড, FINAL_REPORT |
| `tests/` (১৭ ফাইল) | ২৯৭টি টেস্ট — কোড ভাঙলে ধরবে |
| `scripts/` (১০ ফাইল) | setup ও maintenance টুল (`set_webhook`, `check_config`...) |
| `requirements-dev.txt`, `pytest.ini` | টেস্ট চালানোর জন্য |
| `runtime.txt` | পুরোনো ধরনের version pin — Render এটি পড়ে **না**, ক্ষতি নেই |

> 💡 **এইগুলো GitHub-এ রাখলে Render-এ extra RAM লাগে না** — Render শুধু `requirements.txt` ইনস্টল করে, `tests/` বা `docs/` শুধু পড়ে রাখা ফাইল।

### ❌ C. কখনোই upload করবেন না (গোপন / অপ্রয়োজনীয়)

| ফাইল / ফোল্ডার | কেন না |
|---|---|
| **`.env`** | এতে আপনার আসল `BOT_TOKEN`, `GROQ_API_KEY`, ডেটাবেজ password! কেউ পড়ে ফেললে bot hijack হতে পারে |
| `__pycache__/`, `*.pyc` | অটো-বানানো ফাইল, Git-এ দরকার নেই |
| `.pytest_cache/` | টেস্টের টেম্প ফাইল |
| `.venv/` / `venv/` | লোকাল virtual environment (হাজারো অপ্রয়োজনীয় ফাইল) |
| `.idea/`, `.vscode/`, `.DS_Store` | আপনার এডিটরের নিজের সেটিংস |
| `memory_backup.json`, `exports/` | ব্যক্তিগত চ্যাট ডেটা — অন্যের সাথে শেয়ার করবেন না |
| `info-group-ai-bot.zip` | zip-এর ভেতরে আবার zip দরকার নেই |

**উপরের ❌ তালিকাটি `.gitignore`-এ আগেই লেখা আছে** — তাই আপনি নিশ্চিন্তে `git add .` দিতে পারেন, `.env` নিজে থেকেই বাদ পড়বে।

চেক করুন (যেকোনো সময়):
```bash
git status --short              # .env একেবারেই দেখা যাবে না
git ls-files | grep "^.env$"    # কিছু না দেখালেই ঠিক (fatal: no match) ✅
```

---

## ৩. BotFather → BOT_TOKEN নিন

1. Telegram খুলুন → সার্চ: **@BotFather** → **START**
2. পাঠান: `/newbot`
3. **নাম** দিন (যা দেখা যাবে, যেমন `INFO GROUP AI BOT`)
4. **username** দিন — অবশ্যই `bot` দিয়ে শেষ হবে (যেমন `InfoGroupAiBot`)
5. BotFather একটি লাইন দেবে:
   ```
   Use this token to access the HTTP API:
   123456789:AAH...your-token...
   ```
6. **এই token কপি করে নিরাপদে রাখুন** (এটাই `BOT_TOKEN`)। কাউকে দেবেন না, screenshot করবেন না।

**এখানেই privacy mode বন্ধ করে রাখুন** (আগে করলে পরে ঝামেলা কম):
```
/setprivacy  →  আপনার bot সিলেক্ট করুন  →  Disable
```
> কেন? Privacy mode চালু থাকলে bot গ্রুপের সাধারণ মেসেজ **দেখতেই পায় না** — তখন admin-এর কথা মনে রাখা সম্ভব নয়। বিস্তারিত README-র ধাপ ১২-এ।

**এখনই ভালো সময়:** `/setname`, `/setdescription`, `/setabouttext` দিয়ে bot-কে সুন্দর করে সাজিয়ে রাখতে পারেন।

---

## ৪. GROQ_API_KEY নিন

1. [console.groq.com](https://console.groq.com) → **Sign up** (Google দিয়েও চলে)
2. বাঁ দিকের মেনু → **API Keys** → **Create API Key**
3. নাম দিন (যেমন `info-group-bot`) → **Submit**
4. `gsk_...` দিয়ে শুরু হওয়া key **এখনই কপি করুন** — পরে আর দেখানো হয় না (`GROQ_API_KEY`)
5. Free tier-এর rate limit দেখতে: **Limits** পেজ

> 💡 `GROQ_MODEL` বদলানোর দরকার নেই — ডিফল্ট `openai/gpt-oss-120b` ফ্রি tier-এ চলে। বদলাতে চাইলে Render-এর Environment-এ `GROQ_MODEL` লিখে দিলেই হবে, কোড ছুঁতে হবে না।

---

## ৫. Supabase project + DATABASE_URL

1. [supabase.com](https://supabase.com) → **New project**
2. **Name**: `info-group-bot` · **Database Password**: শক্ত password দিন → **কপি করে রাখুন** (পরে দরকার) · **Region**: **Southeast Asia (Singapore)** (বাংলাদেশের জন্য সবচেয়ে কাছের) · **Create new project** (২ মিনিট লাগবে)
3. Project তৈরি হলে উপরের **Connect** বাটনে ক্লিক করুন
4. ⚠️ **সবচেয়ে গুরুত্বপূর্ণ ধাপ:** ট্যাব থেকে **Session pooler** সিলেক্ট করুন — **"Direct connection" নয়!**

   | ট্যাব | কাজ করবে? | কারণ |
   |---|---|---|
   | Direct connection (`db.[REF].supabase.co`) | ❌ **না** | IPv6-only, Render IPv4-only → `Network is unreachable` |
   | **Session pooler** (`aws-0-[REGION].pooler.supabase.com:5432`) | ✅ **হ্যাঁ** | ফ্রি প্ল্যানে IPv4-এ কাজ করে |
   | Transaction pooler (পোর্ট 6543) | ⚠️ চলতে পারে | Session pooler-ই নিরাপদ পছন্দ |

5. URI-টি এমন দেখাবে (কপি করার সময় `[YOUR-PASSWORD]` জায়গায় আপনার আসল password বসবে):
   ```
   postgresql://postgres.[PROJECT-REF]:[YOUR-PASSWORD]@aws-0-[REGION].pooler.supabase.com:5432/postgres
   ```
6. **এই পুরো লিংকটাই `DATABASE_URL`** (নিচে কোথায় বসবে তা দেখুন)

> 🔐 **Password-এ `@ # % &` থাকলে:** এই অক্ষর লিংকের ভেতরে কাজ করে না — [URL-encode](https://www.urlencoder.org/) করে নিন (`@` → `%40`, `#` → `%23`, `%` → `%25`)। অথবা সহজ পথ: Supabase-এ **Reset database password** দিয়ে শুধু অক্ষর-সংখ্যার password দিন।

---

## ৬. Schema চালান (টেবিল তৈরি)

1. Supabase → বাঁ দিকের মেনু → **SQL Editor** → **New query**
2. আপনার extract করা ফোল্ডার থেকে `database/schema.sql` ফাইলটি Notepad/VS Code-এ খুলুন → **পুরো content কপি** → SQL Editor-এ **paste**
3. **Run** চাপুন → **"Success. No rows returned"** দেখবেন ✅

এতে তৈরি হয়: `admin_messages`, `qa_memory`, `bot_users`, `bot_settings`, `pending_questions`, `message_logs`, `conversation_sessions`, `response_cache`, `ai_usage_log` + ইনডেক্স + ভিউ + maintenance function।

> 🔁 এটি **idempotent** — ভুলে আবার চালালেও কোনো ক্ষতি হবে না, error আসবে না।
> চেক করতে: **Table Editor**-এ গিয়ে টেবিলগুলো দেখতে পাবেন।

---

## ৭. GitHub-এ code upload

### পথ A — Git কমান্ড দিয়ে (সুপারিশ করা)

Git ইনস্টল থাকলে terminal/CMD খুলে:

```bash
cd path/to/info-group-ai-bot        # যেখানে extract করেছেন

git init
git add .
git commit -m "INFO GROUP AI BOT: initial commit"
git branch -M main
```

> এইখানে একবার চেক করুন `.env` লিস্টে নেই কি না:
> ```bash
> git status --short
> ```
> `.env` দেখা গেলে সাথে সাথে `git rm --cached .env` দিন।

এবার GitHub-এ যান:

1. [github.com](https://github.com) → লগইন → ডান উপরের **+** → **New repository**
2. **Repository name**: `info-group-ai-bot`
3. **Public** বা **Private** — যেটা চান (Private হলেও Render free-এ deploy হবে)
4. ⚠️ **"Add a README file" / "Add .gitignore" — কোনো টিক দেবেন না** (আপনার নিজের README/.gitignore আছে)
5. **Create repository** → পরের পেজে দেখানো কমান্ড থেকে শুধু শেষ দুটি লাইন চালান:

```bash
git remote add origin https://github.com/আপনার-ইউজারনেম/info-group-ai-bot.git
git push -u origin main
```

প্রথমবার username/password চাইবে → password-এর জায়গায় **Personal Access Token** দিতে হয় ([github.com/settings/tokens](https://github.com/settings/tokens) → *Generate new token (classic)* → scope: `repo` → copy)।

**সফল হলে:** পেজ রিফ্রেশ করলে সব ফাইল দেখবেন। ডান দিকে **"main"** ব্রাঞ্চ লেখা থাকবে।

### পথ B — Git ছাড়া (ওয়েবসাইট থেকে drag & drop)

1. GitHub → **New repository** (উপরে যেমন) → **Create repository**
2. **"uploading an existing file"** লিংকে ক্লিক
3. আপনার ফোল্ডারের ভেতরের **সব ফাইল ও ফোল্ডার সিলেক্ট করে** পেজে **drag & drop** করুন
   (⚠️ `.env` ফাইল কখনো drag করবেন না — থাকলে আগে ডিলিট করুন)
4. নিচে **Commit changes**
5. ✅ **এখন যাচাই করুন:** রিপোতে `.gitignore`, `.env.example`, `.python-version` এই তিনটি ফাইল আছে কি?
   - থাকলে: পারফেক্ট, কাজ শেষ।
   - না থাকলে (ওয়েব আপলোডার লুকানো ফাইল মিস করে): **Add file → Create new file** → নাম লিখুন `.gitignore` → আপনার কম্পিউটারের `.gitignore` ফাইলের content paste → Commit করুন। একইভাবে `.python-version` (ভেতরে শুধু `3.11` লাইন) এবং `.env.example` বানান।

> 📌 **সহজ বিকল্প:** [GitHub Desktop](https://desktop.github.com/) ইনস্টল করে *Add local repository* → *Publish repository* দিলে হিডেন ফাইল নিয়ে কোনো টেনশনই থাকে না।

---

## ৮. Render-এ deploy

1. [dashboard.render.com](https://dashboard.render.com) → **Sign up** (GitHub দিয়ে sign in করলেই সহজ)
2. **New +** → **Web Service**
3. **Build and deploy from a Git repository** → **Next** → আপনার `info-group-ai-bot` রিপো **Connect**
4. নিচের ঘরগুলো **হুবহু** এভাবে ভরুন:

   | Field | যা দিতে হবে |
   |---|---|
   | **Name** | `info-group-ai-bot` (যা খুশি, এটাই হবে আপনার URL) |
   | **Region** | `Singapore` (বাংলাদেশের জন্য দ্রুত) |
   | **Branch** | `main` |
   | **Runtime / Language** | `Python 3` |
   | **Build Command** | `pip install --upgrade pip && pip install -r requirements.txt` |
   | **Start Command** | `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --access-logfile - --error-logfile - app:app` |
   | **Instance Type** | `Free` |
   | **Health Check Path** | `/health` |

   > ⚠️ **Start Command-এ `--workers 1` ইচ্ছাকৃত।** একাধিক worker দিলে Telegram একই মেসেজ দুবার এনে duplicate উত্তর দিতে পারে। Render Free-এ ১ worker + ৪ thread-ই যথেষ্ট।

5. **Environment** সেকশনে গিয়ে **Add Environment Variable** দিয়ে নিচেরগুলো যোগ করুন:

   | Key | Value | কোথায় পাবেন |
   |---|---|---|
   | `BOT_TOKEN` | `123456789:AAH...` | ধাপ ৩ (BotFather) |
   | `GROQ_API_KEY` | `gsk_...` | ধাপ ৪ (Groq) |
   | `GROQ_MODEL` | `openai/gpt-oss-120b` | এটাই ডিফল্ট, চাইলে বদলান |
   | `DATABASE_URL` | `postgresql://postgres.[REF]:[PW]@aws-0-[REGION].pooler.supabase.com:5432/postgres` | ধাপ ৫ (**Session pooler**) |
   | `PUBLIC_URL` | `https://info-group-ai-bot.onrender.com` | ⚠️ **শুধু base URL** — শেষে `/webhook` লিখবেন না (নিচের বক্স দেখুন) |
   | `WEBHOOK_SECRET` | যেকোনো লম্বা random লেখা | নিচের কমান্ড দিয়ে বানান |
   | `TARGET_ADMIN_ID` | `123456789` | যার কথা মনে রাখবে (নিজের id) |
   | `ADMIN_ID` | `123456789` | আপনি (একই id হলে দুবার লিখুন) |
   | `GROUP_ID` | `-1001234567890` | গ্রুপের id (ধাপ ১০) |

   **`WEBHOOK_SECRET` বানানোর কমান্ড:**
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   > ⚠️ **`PUBLIC_URL`-এ ভুল করলে bot একদম চুপ থাকবে — এটি সবচেয়ে কমন ভুল।**
   > কোড নিজেই শেষে `/webhook` যোগ করে। তাই:
   > | আপনি লিখলে | Telegram যে ঠিকানায় পাঠাবে | ফল |
   > |---|---|---|
   > | `https://x.onrender.com` | `https://x.onrender.com/webhook` | ✅ কাজ করবে |
   > | `https://x.onrender.com/` | `https://x.onrender.com/webhook` | ✅ কাজ করবে |
   > | `https://x.onrender.com/webhook` | `https://x.onrender.com/webhook/webhook` | ❌ **404 → bot চুপ** (নতুন version এটি নিজেই ঠিক করে দেয়) |
   >
   > 💡 **Webhook আগে সেট করবেন, না deploy-এর পরে?** — কোনো পার্থক্য নেই। bot প্রতিবার চালু হওয়ার সময় নিজেই Telegram-এ webhook রেজিস্টার করে (`SET_WEBHOOK_ON_STARTUP=true`)। তাই আগে-পরে নিয়ে চিন্তা করবেন না; শুধু **URL ঠিক** হতে হবে।

   **`PUBLIC_URL` কোথায় পাবেন:** service তৈরি করার পর Render উপরে একটি লিংক দেখাবে, যেমন
   `https://info-group-ai-bot-a1b2.onrender.com` → ঠিক এই লিংকটাই `PUBLIC_URL` (শেষে `/` দেবেন না)। নাম আগে থেকে জানা থাকলে `https://<আপনার-service-নাম>.onrender.com` এভাবেও লিখতে পারেন।

6. **Create Web Service** → **Deploy** শুরু হবে (২–৪ মিনিট)
7. **Logs** ট্যাব খুলে রাখুন। সফল হলে দেখবেন:

   ```
   == Starting INFO GROUP AI BOT v1.0.0 (env=production)
   == configuration: {... "database_configured": True ...}
   == database: connected (PostgreSQL 17.x ...), pg_trgm=True, missing tables=none
   == services built: ready=True errors=none
   == handlers registered: 7 public + 7 admin commands
   == Telegram application started (mode=webhook, bot=@আপনার_বট)
   ```
   🎉 **এগুলো দেখলেই bot চালু হয়ে গেছে।**

   > 🔵 `Missing required config: ['TARGET_ADMIN_ID']` টাইপ লাইন দেখলে → Environment-এ variable বাদ পড়েছে, যোগ করে **Manual Deploy → Deploy latest commit** দিন।

---

## ৯. Webhook সেট করা

Telegram-কে বলতে হবে "নতুন মেসেজ আমার Render URL-এ পাঠাও":

**ব্রাউজারে এই লিংকটি খুলুন** (আপনার URL + আপনার secret):

```
https://info-group-ai-bot.onrender.com/set_webhook?token=আপনার_WEBHOOK_SECRET
```

সফল হলে দেখবেন: `{"ok": true, "url": "...", "pending_update_count": 0}` ✅

> ℹ️ আপনার সেটিংসে `SET_WEBHOOK_ON_STARTUP=true` থাকায় bot নিজেই startup-এ webhook সেট করে। তবু এই ধাপটি একবার করে রাখলে নিশ্চিত হওয়া যায়। কোনো সমস্যা হলে `scripts/set_webhook.py --info` দিয়ে চেক করা যায়।

**`.env` লোকাল ফাইল বানানো (লোকালে টেস্ট করতে):**
```bash
cd path/to/info-group-ai-bot
cp .env.example .env        # Windows: copy .env.example .env
```
তারপর `.env` খুলে ধাপ ৮-এর মতো ভ্যালুগুলো বসান, কিন্তু `PUBLIC_URL` ও `WEBHOOK_SECRET` খালি রেখে দিন (লোকালে long-polling চলবে)। **এই `.env` ফাইল কখনো GitHub-এ দেবেন না।**

### 🩺 ৯.৫ — Bot চুপ থাকলে: এক লিংকে রোগনির্ণয় (`/diagnose`)

কিছুই কাজ করছে না বুঝতে পারছেন না? ব্রাউজারে খুলুন:

```
https://আপনার-service.onrender.com/diagnose?token=আপনার_WEBHOOK_SECRET
```

এটি নিজে থেকেই সব পরীক্ষা করে বাংলায় সমাধান বলে দেবে — Telegram-এর সাথে talks করে দেখা হয়:

| চেক | কী দেখে |
|---|---|
| `configuration` · `public_url` | env variable ঠিক আছে কি, Telegram যে URL-এ পাঠাবে সেটাই expected কি না |
| `database` | Supabase-এ connection হচ্ছে কি (Session pooler ঠিক আছে কি) |
| `bot_token` | token বৈধ কি, bot এর username কী |
| `privacy_mode` | ⚠️ **privacy mode বন্ধ আছে কি** — চালু থাকলে bot গ্রুপের কথা দেখতেই পায় না |
| `webhook` | Telegram যে URL-এ পাঠাচ্ছে + `last_error_message` (404 / 403 / 502 হলে কারণ বলে দেয়) |
| `group` · `bot_in_group` | `GROUP_ID` ঠিক কি, bot গ্রুপে আছে কি |
| `target_admin` | কার কথা মনে রাখবে সেটি সেট করা কি |

উত্তরে `"verdict": "healthy"` মানে সব ঠিক ✅ · `"degraded"` মানে চলছে কিন্তু কিছু ঠিক করা দরকার ⚠️ · `"broken"` মানে bot কাজ করবে না ❌ (কারণ + সমাধান `fixes_bn`-এ বাংলায় পাবেন)।

> 🔐 `/diagnose` আপনার `WEBHOOK_SECRET` ছাড়া চলে না এবং কোনো token/key কখনো উত্তরে দেয় না।

**কম্পিউটারে Python থাকলে — এক কমান্ডেই পুরো রিপোর্ট:**
```bash
python scripts/set_webhook.py --info      # সব চেক বাংলা সমাধানসহ
python scripts/set_webhook.py --info --json   # কাঁচা JSON দরকার হলে
```

**শুধু browser দিয়ে দ্রুত চেক (কিছু deploy না করেই):**

1. **আসল Render URL বের করুন:** Render ড্যাশবোর্ড → আপনার service → উপরে দেখানো URL (যেমন `https://info-group-ai-bot-a1b2.onrender.com`) কপি করুন।
2. **Telegram কী ভাবছে দেখুন** (`<TOKEN>` জায়গায় `BOT_TOKEN`):
   `https://api.telegram.org/bot<TOKEN>/getWebhookInfo`
   - `"url"` → Telegram ঠিক যেখানে পাঠাচ্ছে। এতে `/webhook/webhook` থাকলে → `PUBLIC_URL` ঠিক করুন
   - `"last_error_message"` → 404 / 403 / 502 — কেন ব্যর্থ হচ্ছে
3. **Privacy mode দেখুন:** `https://api.telegram.org/bot<TOKEN>/getMe`
   - `"can_read_all_group_messages": false` → privacy **চালু**; BotFather → `/setprivacy` → **Disable** → গ্রুপে bot remove করে আবার add

---

## ১০. গ্রুপে bot যোগ করা + টেস্ট

### গ্রুপ setup

1. তোমার Telegram গ্রুপ খুলুন → গ্রুপ নামে ক্লিক → **Add members** → আপনার bot-এর username লিখে যোগ করুন
2. Bot-কে **admin বানানোর দরকার নেই**, তবে admin বানালে কিছু সুবিধা পাবেন (pin/delete message ability)। সাধারণ member হিসেবেও কাজ করবে।
3. ⚠️ **Privacy mode আগে থেকে বন্ধ না করে থাকলে:** গ্রুপ থেকে bot-কে **remove** করুন → BotFather-এ `/setprivacy` → **Disable** → গ্রুপে **আবার add** করুন। (privacy পরিবর্তন তখনই কাজে লাগে)

### ID বের করা (সবচেয়ে সহজ পথ)

গ্রুপে bot-কে পাঠান: `/whoami`

Bot উত্তর দেবে — কার id কী, কোন variable-এ বসবে:
```
আপনার user id: 123456789        → ADMIN_ID / TARGET_ADMIN_ID
এই chat id     : -1001234567890  → GROUP_ID
Target admins  : [123456789]
```
এই দুটি মান Render-এর Environment-এ বসিয়ে **Save** করুন (Render নিজে থেকে redeploy করবে)।

### কার্যকারিতা টেস্ট (৫ মিনিট)

| টেস্ট | কী করবেন | কী আশা করবেন |
|---|---|---|
| ১ | গ্রুপে লিখুন: `/start` | স্বাগত বার্তা + কমান্ডের তালিকা |
| ২ | `/status` | DB connected, AI enabled, webhook mode দেখাবে |
| ৩ | target admin হিসেবে **একটি মতামত লিখুন**: `BTC 62000 এ support আছে, ভাঙলে 58000 দেখবো` | bot চুপ থাকবে, কিন্তু মনে রাখবে (এটাই কাঙ্ক্ষিত) |
| ৪ | অন্য সদস্য জিজ্ঞেস করুন: `BTC entry নেওয়া যাবে কি?` | bot চুপ থাকবে (mention/reply ছাড়া উত্তর দেয় না) |
| ৫ | এখন লিখুন: `@আপনার_bot_username BTC entry নেওয়া যাবে কি?` | ✅ AI উত্তর আসবে (*"এখনই entry না নেওয়াই ভালো..."* এরকম) + নিচে 🤖 disclosure line |
| ৬ | `/memory_stats` (admin) | admin message কতটি, Q&A pair কতটি, latest memory তারিখ |
| ৭ | কোনো সদস্যের প্রশ্নের **reply দিয়ে** admin উত্তর দিন → পরে একই প্রশ্ন করুন | এই pair-ও memory-তে যাবে (reply pairing) |
| ৮ | `/clear_memory` | আগে preview দেখাবে, তারপর `confirm` চাইবে — ভুলে ডেটা মুছবে না |

> 🤖 bot কখন উত্তর দেয়: `/ask` কমান্ডে, bot-কে mention করলে, bot-এর মেসেজে reply দিলে, বা `TRIGGER_KEYWORDS` থাকলে। **প্রতিটি মেসেজে উত্তর দেয় না** — এটি ইচ্ছাকৃত, নাহলে গ্রুপ spam হয়ে যেত।

---

## ১১. UptimeRobot (ঘুম থেকে বাঁচান)

Render free service **১৫ মিনিট** কেউ না ডাকলে ঘুমিয়ে পড়ে → bot বন্ধ হয়ে যায় (Telegram-এ কোনো উত্তর আসে না)।

1. [uptimerobot.com](https://uptimerobot.com) → Sign up (ফ্রি)
2. **+ Add New Monitor**
   - **Monitor Type**: `HTTPS`
   - **Friendly Name**: `INFO GROUP AI BOT`
   - **URL**: `https://info-group-ai-bot.onrender.com/health`
   - **Monitoring Interval**: `5 minutes` (ফ্রি প্ল্যানে এটি সর্বনিম্ন)
   - (ঐচ্ছিক) **Alert Contacts**: নিজের ইমেইল, যাতে bot ডাউন হলে খবর পান
3. **Create Monitor** → ৫ মিনিট পর পর Render জাগবে, bot চালু থাকবে ✅

> `POST /webhook`-ও bot জাগায়, তবে UptimeRobot দিলে আগেই জাগা থাকে → প্রথম প্রশ্নে দেরি হয় না।

---

## ১২. সব ঠিক হয়েছে কি না — চূড়ান্ত চেকলিস্ট

- [ ] GitHub রিপোতে **সব সোর্স ফাইল** আছে (app.py, ai/, bot_telegram/, utils/, database/, requirements.txt, Procfile, .python-version)
- [ ] GitHub-এ **`.env` নেই** (`git ls-files | grep "^.env$"` → কিছু দেখায় না)
- [ ] Supabase-এ **সব টেবিল** দেখা যাচ্ছে (Table Editor)
- [ ] Render log-এ `database: connected` ও `handlers registered: 7 public + 7 admin commands`
- [ ] `/health` খুললে `"status": "ok"` এবং `"telegram": {"started": true}`
- [ ] `/set_webhook?token=...` → `{"ok": true}`
- [ ] `/diagnose?token=...` → `"verdict": "healthy"` (privacy mode, webhook URL, DB সব সবুজ)
- [ ] গ্রুপে `/whoami` ও `/status` কাজ করছে
- [ ] Admin একটা কথা বলার পর `/memory_stats`-এ সংখ্যা বাড়ছে
- [ ] Mention করে প্রশ্ন করলে AI উত্তর আসছে
- [ ] UptimeRobot monitor **Up** দেখাচ্ছে

---

## ১৩. কিছু ভুল হলে — দ্রুত সমাধান

| যা দেখছেন | কারণ | সমাধান |
|---|---|---|
| Build fail: `psycopg2` compile error | Render Python 3.14 পেয়েছে | রিপোতে `.python-version` ফাইল আছে কি? (ভেতরে `3.11`) না থাকলে বানান, তারপর **Manual Deploy** |
| log: `Network is unreachable` / `no tenant identifier provided` | Direct connection URI ব্যবহার করেছেন | Supabase **Connect → Session pooler** URI নিন (port `5432`, user `postgres.REF`) |
| log: `Missing required config: [...]` | env variable বাদ পড়েছে | Render → Environment → যোগ করুন → redeploy |
| log: `The token ***REDACTED*** was rejected` | `BOT_TOKEN` ভুল/মুছে ফেলা | BotFather থেকে token আবার কপি করুন (নতুন token চাইলে `/revoke`) |
| গ্রুপে bot কিছুই করে না | Privacy mode চালু, বা bot গ্রুপে নেই | `/diagnose?token=...` খুলুন → `privacy_mode` ও `bot_in_group` চেক দেখুন; `/setprivacy` → Disable → remove + re-add bot |
| `{"ok": false, "error": "not found"}` | ভুল path-এ ঢুকেছেন (টাইপো/অতিরিক্ত `/`) | `/diagnose?token=...` বা `/set_webhook?token=...` হুবহু লিখুন; ভুলটা করলে এখন তালিকা সহ বার্তা দেখাবে |
| প্রশ্ন করলে উত্তর আসে না, কিন্তু `/status` ঠিক | question-এ mention/reply নেই, বা memory খালি | bot-কে mention করুন; admin আগে কিছু লিখেছেন কি না দেখুন (`/memory_stats`) |
| Render service ঘুমিয়ে পড়ছে | UptimeRobot নেই | ধাপ ১১ করুন |
| `409 Conflict` / duplicate উত্তর | পুরোনো polling instance এখনো চলছে | Render-এ ১টির বেশি service নেই তো? অতিরিক্ত আগের service delete/suspend করুন |
| AI বলে "এই তথ্য আমার কাছে নেই" | memory-তে মিল নেই | admin আসল কথা বলুন, তারপর আবার জিজ্ঞেস করুন; বিস্তারিত: `docs/TROUBLESHOOTING.md` |

আরও বিস্তারিত: **`docs/TROUBLESHOOTING.md`** (symptom → কারণ → সমাধান টেবিল) এবং **README.md**-র Troubleshooting সেকশন।

---

## ১৪. পরে আপডেট করতে চাইলে

```bash
# কোডে পরিবর্তন করার পর
git add .
git commit -m "কী বদলালাম"
git push
```
`render.yaml`-এ `autoDeploy: true` আছে, তাই push করলেই Render নিজে থেকে নতুন version deploy করবে।

**শুধু setting বদলাতে চাইলে** (Render → Environment) → Save → Render নিজেই redeploy করবে — কোড push করা লাগবে না।

**নতুন ফিচার যোগ করার আগে পরীক্ষা:**
```bash
pytest -q                        # ২৯৭টি টেস্ট
python scripts/check_config.py   # config যাচাই
python scripts/simulate_conversation.py -v   # Telegram ছাড়া পুরো pipeline ডেমো
```

**ব্যাকআপ (memory মুছে ফেলার আগে ভালো অভ্যাস):**
```bash
python scripts/export_memory.py --format md --out memory_backup.md
```

---

## 💰 ফ্রি প্ল্যানের সীমা (জেনে রাখুন)

| সার্ভিস | সীমা | প্রভাব |
|---|---|---|
| Render Free | ৫১২ MB RAM, ১৫ মিনিট idle-এ ঘুম | UptimeRobot দিয়ে সমাধান (ধাপ ১১) |
| Supabase Free | ৫০০ MB ডেটাবেজ, ৭ দিন নিষ্ক্রিয় থাকলে pause | bot চালু থাকলে কখনো pause হবে না |
| Groq Free | rate limit (মিনিট/দিন ভিত্তিক) | বেশি প্রশ্নে `429` → bot সুন্দর বার্তা দেবে, crash করবে না; `CACHE_ENABLED=true` থাকায় একই প্রশ্নে AI call নষ্ট হয় না |
| UptimeRobot Free | ৫০ monitor, ৫ মিনিট interval | যথেষ্ট |

---

## 🎯 এক লাইনে সারসংক্ষেপ

**ZIP extract → BotFather থেকে token → Groq key → Supabase (Session pooler!) + schema.sql → GitHub push → Render web service + ৯টি env variable → `/set_webhook?token=...` → গ্রুপে bot add + `/whoami` → UptimeRobot ৫ মিনিট।**

কোনো ধাপে আটকে গেলে: `docs/TROUBLESHOOTING.md` · `README.md` · `docs/DEPLOYMENT.md` — তিনটিতেই বিস্তারিত আছে।

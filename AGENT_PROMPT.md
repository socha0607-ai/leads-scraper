# Project: Scalable Intent-Lead Scraper & AI Qualification Bot

## 🎯 Objective
Build a production-ready, modular Python application that monitors targeted subreddits for high-intent B2B leads (specifically founders/businesses looking to hire Web/MVP Developers or AI Automation Agencies), filters out noise/job-seekers using Gemini AI, drafts a custom outreach pitch, and sends instant alerts via Telegram.

## 📁 Folder Structure to Create
Create the following clean, scalable directory structure:
```text
intent-lead-scraper/
├── .env.example
├── .env
├── requirements.txt
├── config.py
├── database.py
├── scraper.py
├── ai_filter.py
├── notifier.py
└── main.py
```

## 🛠️ File Specifications & Implementation Details

### 1. `requirements.txt`
Include:
- `requests`
- `google-genai`
- `python-dotenv`
- `schedule`

### 2. `.env` & `.env.example`
Create both files with these exact keys:
```env
GEMINI_API_KEY=your_gemini_api_key_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
CHECK_INTERVAL_MINUTES=15
```

### 3. `config.py`
Configure high-intent B2B target parameters:
- **TARGET_SUBREDDITS:** `["SaaS", "Entrepreneur", "startups", "smallbusiness", "forhire", "freelance_forhire", "Automate"]`
- **INTENT_KEYWORDS:** `["looking for developer", "need a developer", "hire a dev", "build an mvp", "need an mvp", "looking for agency", "automate workflow", "ai automation", "need a website", "looking for cofounder"]`
- **USER_AGENT:** Custom realistic browser User-Agent string to prevent Reddit `429 Too Many Requests` blocks.

### 4. `database.py` (SQLite Deduplication)
- Initialize a local SQLite database `leads.db` with a `processed_posts` table (`post_id TEXT PRIMARY KEY`, `subreddit TEXT`, `created_at TIMESTAMP`).
- Provide helper functions: `is_post_processed(post_id)` and `mark_post_processed(post_id, subreddit)`.

### 5. `scraper.py` (Reddit JSON Engine)
- Fetch the latest 25 posts from each subreddit in `TARGET_SUBREDDITS` using Reddit's public JSON endpoint (`https://www.reddit.com/r/{sub}/new.json?limit=25`).
- Add a polite `time.sleep(2)` between subreddit requests to avoid rate limits.
- Filter posts whose `title` or `selftext` matches any phrase in `INTENT_KEYWORDS` (case-insensitive) and skip posts already in `leads.db`.

### 6. `ai_filter.py` (Gemini Qualification & Pitch Generator)
- Use the official `google-genai` SDK with model `gemini-2.5-flash`.
- Pass the post title, body, and subreddit to Gemini with a strict JSON output schema containing:
  - `is_buyer` (boolean): `true` ONLY if the author is actively looking to hire/pay for a service. `false` if they are a freelancer promoting themselves (`[For Hire]`), spamming, or just asking a general question.
  - `intent_score` (integer 1-10): How urgent and high-budget the lead looks.
  - `summary` (string): 1-line summary of what the client needs.
  - `custom_dm` (string): A short, non-salesy, helpful 3-sentence outreach message tailored to their exact problem.

### 7. `notifier.py` (Telegram Alert Formatter)
- Send formatted HTML messages via Telegram Bot API (`sendMessage`) only when `is_buyer == True` and `intent_score >= 7`.
- Include: Subreddit, Intent Score, Summary, Direct Post Link, and the ready-to-copy Custom DM.

### 8. `main.py` (Orchestrator)
- Load `.env`, initialize `database.py`, run the full pipeline once immediately on startup, and then schedule it to run every `CHECK_INTERVAL_MINUTES` using a `while True` loop with proper exception handling so it never crashes on network hiccups.

## ✅ Execution Instructions for Agent
1. Generate all the files listed above with complete, error-handled, production-grade Python code.
2. Create a Python virtual environment (`venv`) and install `requirements.txt`.
3. Verify syntax across all `.py` files (`python -m py_compile *.py`).
4. Leave `.env` ready for the user to paste their API keys and run `python main.py`.

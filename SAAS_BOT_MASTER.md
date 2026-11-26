# Project Upgrade: Multi-User Telegram SaaS Bot & Admin Growth Engine

## 🎯 Objective
Upgrade the existing `intent-lead-scraper` into a complete, multi-tenant **Telegram SaaS Application** with:
1. **Interactive User Onboarding (`/start`):** Inline keyboard buttons allowing users to select their target industry/niche (`Web & MVP Dev`, `AI Automation`, `SEO & Marketing`, `Video Editing`).
2. **5-Day Free Trial & Subscription Gatekeeping:** Automatic trial tracking in SQLite (`users` table) that blocks alerts once the 5-day trial expires unless upgraded to `pro`.
3. **Niche-Routed Lead Delivery:** Scraping Reddit (`old.reddit.com` via Playwright) across all niches and routing qualified leads ONLY to active users subscribed to that specific niche.
4. **Admin Growth Scraper (For Owner):** A separate scraper profile targeting agency owners struggling to get clients, generating tailored outreach replies so the owner can acquire new SaaS users.

## 📁 Updated Folder Structure
```text
intent-lead-scraper/
├── .env
├── requirements.txt
├── config.py          # Niche definitions + Admin customer-acquisition keywords
├── database.py        # SQLite tables: `users` (with trial tracking) & `processed_posts`
├── bot_handler.py     # Interactive Telegram bot (/start menu, niche buttons, /status, /admin_add)
├── scraper.py         # Playwright headless scraper for old.reddit.com
├── ai_filter.py       # Gemini 2.5 Flash qualification & custom pitch generator
├── notifier.py        # Niche-based alert router & Admin growth alert sender
└── main.py            # Concurrent runner (Telegram polling thread + Scraper scheduler loop)
```

## 🛠️ Implementation Specifications

### 1. `requirements.txt`
Ensure these packages are listed and installed:
- `playwright`
- `google-genai`
- `python-dotenv`
- `schedule`
- `pyTelegramBotAPI` (for clean, reliable inline buttons and `/start` command handling)

### 2. `.env` Variables
Keep existing keys and ensure `ADMIN_CHAT_ID` is supported:
```env
GEMINI_API_KEY=your_gemini_api_key_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
ADMIN_CHAT_ID=your_telegram_chat_id_here
CHECK_INTERVAL_MINUTES=15
```

### 3. `config.py` (Niche & Admin Mapping)
Define `NICHES` dictionary mapping niche keys to their subreddits and intent keywords:
- `"web_dev"`: Subreddits `["SaaS", "startups", "Entrepreneur", "forhire", "freelance_forhire"]`, Keywords `["looking for developer", "need a developer", "build an mvp", "need a website", "hire a dev"]`
- `"ai_auto"`: Subreddits `["Automate", "smallbusiness", "Entrepreneur", "SaaS"]`, Keywords `["ai automation", "automate workflow", "looking for agency", "need ai agent", "zapier expert"]`
- `"seo_mkt"`: Subreddits `["smallbusiness", "Entrepreneur", "marketing", "forhire"]`, Keywords `["need seo", "looking for marketing agency", "google ads help", "need lead generation"]`
- `"admin_growth"`: (Internal Admin-Only Niche for user acquisition) Subreddits `["agency", "freelance", "webdev", "SEO", "marketing"]`, Keywords `["how to get clients", "struggling to find clients", "cold email not working", "need more leads", "first agency client"]`

### 4. `database.py` (Users, Trials & Deduplication)
Create/update SQLite database `leads.db` with two tables:
- `processed_posts (post_id TEXT PRIMARY KEY, niche TEXT, created_at TIMESTAMP)`
- `users (chat_id INTEGER PRIMARY KEY, username TEXT, niche TEXT, plan TEXT DEFAULT 'trial', trial_ends_at TIMESTAMP, created_at TIMESTAMP)`
Implement clean functions:
- `register_or_update_niche(chat_id, username, niche)`: Inserts new user with `plan='trial'` and `trial_ends_at = now + 5 days` (if new), or updates `niche` if existing.
- `get_active_users_by_niche(niche)`: Returns list of `chat_id`s where `plan == 'pro'` OR (`plan == 'trial'` AND `trial_ends_at > current_utc_time`).
- `get_user_status(chat_id)`: Returns user's niche, plan, and remaining trial days.
- `upgrade_user_to_pro(chat_id, days=30)`: Sets `plan='pro'` and extends expiry.

### 5. `bot_handler.py` (Interactive Telegram Menu)
Using `telebot` (`pyTelegramBotAPI`):
- **`/start` command:** Greets the user, explains the **5-Day Free Trial** of real-time high-intent Reddit leads, and presents an `InlineKeyboardMarkup` with buttons:
  - `💻 Web & MVP Dev` (`callback_data="niche_web_dev"`)
  - `🤖 AI Automation` (`callback_data="niche_ai_auto"`)
  - `📈 SEO & Marketing` (`callback_data="niche_seo_mkt"`)
- **Callback Handler:** Saves the chosen niche via `register_or_update_niche()`, confirms their 5-day free trial is active, and tells them leads will arrive automatically.
- **`/status` command:** Shows their active niche, plan status (`Trial` or `Pro`), and days remaining. If expired, displays a placeholder payment/upgrade message (`Contact @Admin or use payment link to unlock Pro`).
- **`/upgrade <chat_id>` command (Admin only):** Checks if sender `chat_id == ADMIN_CHAT_ID`, upgrades the target user to `pro`, and notifies the user.

### 6. `scraper.py` & `ai_filter.py`
- Update `scraper.py` (using Playwright on `old.reddit.com`) to iterate through all keys in `NICHES`, tagging each scraped post with its `niche`.
- Update `ai_filter.py` (`gemini-2.5-flash`):
  - If `niche != "admin_growth"`: Qualify if the author is a real buyer (`is_buyer: true`, `intent_score >= 7`) and draft a helpful 3-sentence outreach DM for the agency user.
  - If `niche == "admin_growth"`: Qualify if the author is an agency owner/freelancer looking for clients (`is_buyer: true`, `intent_score >= 7`), and draft a tailored, non-spammy comment/DM offering a free 5-day trial of this Telegram Lead Bot (`t.me/<bot_username>`).

### 7. `notifier.py` & `main.py`
- In `notifier.py`:
  - For regular niches, fetch `get_active_users_by_niche(niche)` and send the formatted HTML lead alert + custom DM draft to each active user.
  - For `"admin_growth"`, send the alert ONLY to `ADMIN_CHAT_ID` with the label `🔥 NEW SAAS CUSTOMER LEAD`.
- In `main.py`:
  - Run `bot.infinity_polling()` in a background `threading.Thread(daemon=True)` so users can click `/start` anytime without blocking the Playwright scraping loop in the main thread.

## ✅ Execution & Verification Steps for Agent
1. Generate/update all files cleanly with proper exception handling and logging.
2. Install `pyTelegramBotAPI` and verify `playwright` chromium is installed.
3. Run `python -m py_compile *.py` to confirm zero syntax errors.
4. Start `python main.py` briefly to verify both the Telegram bot polling and Playwright scraper initialize without errors.

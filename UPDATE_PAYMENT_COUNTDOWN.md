# Task: Add Live Trial Countdown, Dodo Payments Checkout Button & Broaden Keywords

## 🎯 Goal
Enhance `bot_handler.py`, `database.py`, `config.py`, and `.env` so users see their exact remaining subscription time in the `/start` menu, get a direct Dodo Payments upgrade button, and capture more real-world leads.

## 🛠️ Required Changes

### 1. Update `.env`
Add a placeholder variable for the Dodo Payments checkout URL:
```env
DODO_PAYMENT_LINK=[https://checkout.dodopayments.com/buy/your_product_link_here](https://checkout.dodopayments.com/buy/your_product_link_here)
```

### 2. Update `database.py`
Enhance `get_user_status(chat_id)` to return exact remaining time:
- Calculate `time_left = trial_ends_at - datetime.utcnow()`.
- Return a dictionary with `exists` (bool), `niche` (str), `plan` (str), `is_active` (bool), `days_left` (int), and `hours_left` (int).

### 3. Update `bot_handler.py`
- **On `/start` command:**
  - Check `get_user_status(chat_id)`.
  - If the user already exists, display a clean status banner at the top of the welcome message:
    - If `is_active`: `🟢 Plan: {plan.upper()} | ⏳ Remaining: {days_left} days, {hours_left} hours left`
    - If expired: `🔴 Plan: EXPIRED | Upgrade below to resume real-time leads.`
  - Below the niche selection buttons (`💻 Web & MVP Dev`, `🤖 AI Automation`, `📈 SEO & Marketing`), always include a clean inline URL button:
    - `💳 Upgrade to Pro ($79/mo)` -> links directly to `os.getenv("DODO_PAYMENT_LINK")`.
- **When a user upgrades (`/upgrade <chat_id>`):**
  - Automatically notify the user on Telegram: `"🎉 Your Pro subscription is now active for 30 days!"`

### 4. Broaden `INTENT_KEYWORDS` in `config.py`
Ensure keywords match natural human phrasing on Reddit (not overly rigid phrases) so the scraper catches real opportunities:
- Add broader high-intent phrases like `"looking for a dev"`, `"need a developer"`, `"looking to hire"`, `"anyone know a good"`, `"recommend a"`, `"need help building"`, `"automate my"` across the respective niches.

## ✅ Verification
1. Run `python -m py_compile *.py` to confirm zero syntax errors.
2. Restart `venv/bin/python main.py` and verify it runs cleanly.

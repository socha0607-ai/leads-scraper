"""
bot_handler.py — Interactive Telegram SaaS Bot interface using pyTelegramBotAPI.

Handles:
  - /start               : Live trial/pro countdown banner + niche buttons + Dodo checkout button
  - niche_* callbacks    : Register/update user niche and confirm active trial
  - /status              : Show active niche, plan, and exact remaining time (or upgrade button)
  - /upgrade <chat_id>   : Admin-only command to upgrade a user to Pro & notify them
"""

import logging
import os
import socket
import threading
import time
from dotenv import load_dotenv

load_dotenv(override=True)

import telebot
from telebot import apihelper, types
import urllib3.util.connection as urllib3_cn

# Force IPv4 for requests / telebot.apihelper to avoid IPv6 Read timed out on Indian ISPs
urllib3_cn.allowed_gai_family = lambda: socket.AF_INET
apihelper.READ_TIMEOUT = 30
apihelper.CONNECT_TIMEOUT = 15

from config import (
    ADMIN_CHAT_ID,
    DODO_PAYMENT_LINK,
    NICHES,
    NICHE_LABELS,
    TELEGRAM_BOT_TOKEN,
    TRIAL_DAYS,
)
from database import (
    get_user_status,
    register_or_update_niche,
    upgrade_user_to_pro,
)

logger = logging.getLogger(__name__)

bot = telebot.TeleBot(os.getenv("TELEGRAM_BOT_TOKEN") or TELEGRAM_BOT_TOKEN, parse_mode="HTML")

_BOT_USERNAME_CACHE: str | None = None


def get_bot_username() -> str:
    """Return the bot's Telegram username (cached after first API call)."""
    global _BOT_USERNAME_CACHE
    if _BOT_USERNAME_CACHE:
        return _BOT_USERNAME_CACHE
    try:
        me = bot.get_me()
        if me and me.username:
            _BOT_USERNAME_CACHE = me.username
            return _BOT_USERNAME_CACHE
    except Exception as exc:
        logger.debug("Could not fetch bot username via get_me(): %s", exc)
    return "IntentLeadSaaSBot"


def start_polling() -> None:
    """Clear any stale webhook, verify connectivity via get_me(), and run infinity_polling."""
    global _BOT_USERNAME_CACHE
    try:
        bot.remove_webhook()
        logger.info("Cleared any stale Telegram webhook.")
    except Exception as exc:
        logger.warning("Could not remove webhook: %s", exc)

    try:
        me = bot.get_me()
        if me and me.username:
            _BOT_USERNAME_CACHE = me.username
            logger.info("Telegram bot running as @%s", me.username)
        else:
            logger.info("Telegram bot connected (username unavailable).")
    except Exception as exc:
        logger.warning("Telegram startup get_me() failed: %s", exc)

    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=15, skip_pending=True)
        except Exception as exc:
            logger.error("Telegram polling error: %s — restarting in 5s…", exc)
            time.sleep(5)


def _get_payment_url() -> str:
    """Return the Dodo Payments checkout URL from env/config."""
    return (os.getenv("DODO_PAYMENT_LINK") or DODO_PAYMENT_LINK).strip()


def _build_niche_keyboard() -> types.InlineKeyboardMarkup:
    """Build the inline keyboard with niche buttons + Dodo Payments Pro upgrade URL."""
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("💻 Web & MVP Dev", callback_data="niche_web_dev"),
        types.InlineKeyboardButton("🤖 AI Automation", callback_data="niche_ai_auto"),
        types.InlineKeyboardButton("📈 SEO & Marketing", callback_data="niche_seo_mkt"),
        types.InlineKeyboardButton("💳 Upgrade to Pro ($79/mo)", url=_get_payment_url()),
    )
    return markup


@bot.message_handler(commands=["start"])
def handle_start(message: types.Message) -> None:
    """Greet the user, show live subscription status if registered, and present niche/upgrade buttons."""
    first_name = message.from_user.first_name if message.from_user else "there"
    status = get_user_status(message.chat.id)

    status_banner = ""
    if status["exists"]:
        if status["is_active"]:
            status_banner = (
                f"🟢 <b>Plan:</b> {status['plan'].upper()} | "
                f"⏳ <b>Remaining:</b> {status['days_left']} days, {status['hours_left']} hours left\n\n"
            )
        else:
            status_banner = (
                "🔴 <b>Plan:</b> EXPIRED | Upgrade below to resume real-time leads.\n\n"
            )

    text = (
        f"{status_banner}"
        f"👋 <b>Welcome, {first_name}!</b>\n\n"
        f"Get <b>real-time, AI-qualified B2B leads</b> across Reddit, X, Hacker News, "
        f"Indie Hackers &amp; Product Hunt — complete with intent scores and ready-to-send custom DMs.\n\n"
        f"🎁 Every new user starts with a <b>{TRIAL_DAYS}-Day Free Trial</b>.\n\n"
        f"👇 <b>Choose your target niche below to start receiving leads:</b>"
    )
    bot.send_message(
        message.chat.id,
        text,
        reply_markup=_build_niche_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("niche_"))
def handle_niche_selection(call: types.CallbackQuery) -> None:
    """Save the user's chosen niche and confirm their 5-day free trial is active."""
    niche = call.data.removeprefix("niche_")
    if niche not in NICHE_LABELS or niche == "admin_growth":
        bot.answer_callback_query(call.id, "Unknown niche selection.")
        return

    chat_id = call.message.chat.id
    username = (call.from_user.username or call.from_user.first_name or "") if call.from_user else ""

    register_or_update_niche(chat_id=chat_id, username=username, niche=niche)
    status = get_user_status(chat_id)
    niche_label = NICHE_LABELS.get(niche, niche)
    keywords = NICHES.get(niche, {}).get("keywords", [])
    keywords_text = "\n".join(f"• <code>{kw}</code>" for kw in keywords)

    bot.answer_callback_query(call.id, f"Selected: {niche_label}")

    if status["is_active"]:
        plan_text = "PRO" if status["plan"] == "pro" else f"{TRIAL_DAYS}-Day Free Trial"
        confirm_text = (
            f"✅ <b>Niche Set:</b> {niche_label}\n"
            f"🟢 <b>Plan:</b> {plan_text} | ⏳ <b>Remaining:</b> {status['days_left']} days, {status['hours_left']} hours left\n\n"
            f"🔍 <b>Active Keywords Tracked:</b>\n{keywords_text}\n\n"
            f"Your scraper is active! High-intent leads will arrive here automatically.\n"
            f"Use /status anytime to check your plan or /start to switch niches."
        )
    else:
        confirm_text = (
            f"✅ <b>Niche Updated:</b> {niche_label}\n\n"
            f"🔴 <b>Plan:</b> EXPIRED | Upgrade below to resume real-time leads.\n\n"
            f"🔍 <b>Keywords for this niche:</b>\n{keywords_text}"
        )

    bot.edit_message_text(
        confirm_text,
        chat_id=chat_id,
        message_id=call.message.message_id,
        reply_markup=_build_niche_keyboard(),
    )


@bot.message_handler(commands=["status"])
def handle_status(message: types.Message) -> None:
    """Show the user's active niche, plan status, and exact remaining time."""
    status = get_user_status(message.chat.id)
    if not status["exists"]:
        bot.send_message(
            message.chat.id,
            "ℹ️ You haven't selected a niche yet. Send /start to activate your 5-Day Free Trial!",
        )
        return

    niche_label = NICHE_LABELS.get(status["niche"], status["niche"])

    if status["is_active"]:
        text = (
            f"📊 <b>Your Account Status</b>\n\n"
            f"🎯 <b>Active Niche:</b> {niche_label}\n"
            f"🟢 <b>Plan:</b> {status['plan'].upper()}\n"
            f"⏳ <b>Remaining:</b> {status['days_left']} days, {status['hours_left']} hours left\n\n"
            f"Send /start if you want to switch your target niche."
        )
        bot.send_message(message.chat.id, text)
    else:
        text = (
            f"📊 <b>Your Account Status</b>\n\n"
            f"🎯 <b>Niche:</b> {niche_label}\n"
            f"🔴 <b>Plan:</b> EXPIRED\n\n"
            f"🔒 Your 5-day free trial has ended. Upgrade below to resume real-time leads.\n"
            f"Your Chat ID: <code>{message.chat.id}</code>"
        )
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("💳 Upgrade to Pro ($79/mo)", url=_get_payment_url()))
        bot.send_message(message.chat.id, text, reply_markup=markup)


@bot.message_handler(commands=["upgrade", "admin_add"])
def handle_admin_upgrade(message: types.Message) -> None:
    """Admin-only command: /upgrade <chat_id> [days] to activate Pro for a user."""
    if not ADMIN_CHAT_ID or str(message.chat.id) != str(ADMIN_CHAT_ID):
        bot.send_message(message.chat.id, "⛔ Unauthorized. This command is for the admin only.")
        return

    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "Usage: <code>/upgrade &lt;chat_id&gt; [days]</code>",
        )
        return

    try:
        target_chat_id = int(parts[1])
        days = int(parts[2]) if len(parts) >= 3 else 30
    except ValueError:
        bot.send_message(message.chat.id, "❌ Invalid chat_id or days. Must be integers.")
        return

    if not upgrade_user_to_pro(target_chat_id, days=days):
        bot.send_message(
            message.chat.id,
            f"❌ User <code>{target_chat_id}</code> not found. They must run /start first.",
        )
        return

    bot.send_message(
        message.chat.id,
        f"✅ Upgraded user <code>{target_chat_id}</code> to <b>Pro</b> for {days} days.",
    )

    try:
        bot.send_message(
            target_chat_id,
            f"🎉 Your Pro subscription is now active for {days} days!",
        )
    except Exception as exc:
        logger.warning("Upgraded %s, but failed to notify user: %s", target_chat_id, exc)


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@bot.message_handler(commands=["test_scrape"])
def handle_test_scrape(message: types.Message) -> None:
    """Immediately run a 5-platform live test scrape and report per-platform status."""
    chat_id = message.chat.id
    status = get_user_status(chat_id)
    niche = status["niche"] if status["exists"] and status["niche"] in NICHES else "seo_mkt"

    bot.send_message(
        chat_id,
        f"🔎 Running live 5-platform test scrape (Reddit, Hacker News, Indie Hackers, Product Hunt, X) "
        f"for <b>{NICHE_LABELS.get(niche, niche)}</b>…\n"
        f"Please wait ~15–25 seconds.",
    )

    def _worker() -> None:
        from scraper import run_test_scrape

        res = run_test_scrape(niche=niche)
        if res.get("error"):
            bot.send_message(
                chat_id,
                f"❌ <b>Test Scrape Failed</b>\n\nError: <code>{_escape_html(res['error'])}</code>",
            )
            return

        status_lines = []
        for d in res["details"]:
            badge = d.get("badge") or d.get("platform", "Unknown")
            if d.get("status") == "Skipped":
                status_lines.append(
                    f"• {badge}: ⏭️ Skipped ({_escape_html(d.get('error') or 'not configured')})"
                )
            elif d.get("error") and not d.get("posts_scanned"):
                status_lines.append(
                    f"• {badge}: ❌ {_escape_html(str(d['error'])[:100])}"
                )
            else:
                http_str = f"HTTP <b>{d['status']}</b>" if d.get("status") else "Done"
                status_lines.append(
                    f"• {badge}: {http_str} | {d['posts_scanned']} scanned | {d['matches_found']} matches"
                )

        summary = (
            f"🧪 <b>5-Platform Live Scrape Report</b>\n"
            f"🎯 <b>Niche:</b> {NICHE_LABELS.get(niche, niche)}\n"
            f"🔍 <b>Keywords:</b> <code>{_escape_html(', '.join(res['keywords'][:6]))}</code>\n\n"
            + "\n".join(status_lines)
            + f"\n\n📊 <b>Total Scanned:</b> {res['total_scanned']} | <b>Matches:</b> {res['total_matches']}\n"
        )

        if res["matches"]:
            from notifier import _format_platform_badge

            summary += "\n🔥 <b>Top Matching Posts:</b>\n"
            bot.send_message(chat_id, summary, disable_web_page_preview=True)

            for idx, m in enumerate(res["matches"][:3], start=1):
                badge = _format_platform_badge(m)
                url = _escape_html(m.get("url", ""))
                card = (
                    f"<b>#{idx} Match</b> (keyword: <code>{_escape_html(m['matched_keyword'])}</code>)\n"
                    f"{badge}\n\n"
                    f"📝 <b>Title:</b> {_escape_html(m.get('title', ''))}\n"
                    f"📄 <b>Description:</b> {_escape_html(m.get('description', '(no description)'))}\n\n"
                    f"💬 <b>Suggested Public Comment:</b>\n"
                    f"<pre>{_escape_html(m.get('suggested_comment', ''))}</pre>\n\n"
                    f"✉️ <b>Suggested Direct Message (DM):</b>\n"
                    f"<pre>{_escape_html(m.get('custom_dm', ''))}</pre>\n\n"
                    f"🔗 <b>Direct Link:</b> <a href=\"{url}\">{url}</a>"
                )
                bot.send_message(chat_id, card[:4090], disable_web_page_preview=True)
            return
        else:
            summary += (
                "\nℹ️ <b>0 keyword matches found</b> among the latest posts scanned. "
                "Connectivity and DOM parsing are healthy wherever HTTP 200 &gt; 0 scanned appeared above."
            )

        bot.send_message(chat_id, summary, disable_web_page_preview=True)

    threading.Thread(target=_worker, name=f"test-scrape-{chat_id}", daemon=True).start()

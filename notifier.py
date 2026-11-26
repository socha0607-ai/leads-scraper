"""
notifier.py — Niche-based alert router & Daily 50-Outreach Admin Dispatcher.

Routes qualified items (`is_buyer=True` and `intent_score >= MIN_INTENT_SCORE`):
  - `agency_prospect` / `distributor_prospect` → Sent to ADMIN_CHAT_ID up to
    DAILY_OUTREACH_TARGET (50/day), formatted with live daily progress counter.
  - `client_lead` → Sent to all active (trial or pro) users subscribed to `niche`.
"""

import logging
import requests

from config import (
    ADMIN_CHAT_ID,
    DAILY_OUTREACH_TARGET,
    MIN_INTENT_SCORE,
    NICHE_LABELS,
    TELEGRAM_BOT_TOKEN,
)
from database import (
    get_active_users_by_niche,
    get_today_admin_outreach_count,
    record_admin_outreach,
)

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_MSG_LEN = 4096


def send_lead_alert(post: dict, qualification: dict) -> int:
    """
    Route a qualified lead or global outreach prospect to Telegram recipients.
    Returns the number of messages successfully delivered.
    """
    if not qualification.get("is_buyer"):
        logger.debug("Post %s skipped: not a qualified match.", post.get("post_id"))
        return 0

    score = qualification.get("intent_score", 0)
    if score < MIN_INTENT_SCORE:
        logger.debug(
            "Post %s skipped: intent score %d < threshold %d.",
            post.get("post_id"),
            score,
            MIN_INTENT_SCORE,
        )
        return 0

    category = post.get("category", "client_lead")
    niche = post.get("niche", "web_dev")

    if category in ("agency_prospect", "distributor_prospect") or niche == "admin_growth":
        if not ADMIN_CHAT_ID:
            logger.warning("ADMIN_CHAT_ID not configured; skipping admin outreach alert.")
            return 0

        current_count = get_today_admin_outreach_count()
        if current_count >= DAILY_OUTREACH_TARGET:
            logger.info(
                "Daily admin outreach cap reached (%d/%d). Skipping %s.",
                current_count,
                DAILY_OUTREACH_TARGET,
                post.get("post_id"),
            )
            return 0

        next_count = current_count + 1
        message = _format_admin_outreach_message(post, qualification, next_count)
        if _send_telegram_message(ADMIN_CHAT_ID, message):
            record_admin_outreach(
                post_id=post.get("post_id", ""),
                category=category,
                platform=post.get("platform", "Reddit"),
            )
            return 1
        return 0

    recipients = get_active_users_by_niche(niche)
    if not recipients:
        logger.info("No active users subscribed to niche '%s'; skipping alert.", niche)
        return 0

    message = _format_client_lead_message(post, qualification)
    sent_count = 0
    for chat_id in recipients:
        if _send_telegram_message(chat_id, message):
            sent_count += 1

    logger.info(
        "Delivered [%s] lead %s to %d/%d active user(s).",
        niche,
        post.get("post_id"),
        sent_count,
        len(recipients),
    )
    return sent_count


def _format_admin_outreach_message(post: dict, q: dict, today_count: int) -> str:
    """Format the Global 50/Day Outreach Prospect alert for ADMIN_CHAT_ID."""
    platform = _escape_html(post.get("platform", "Reddit"))
    category = _escape_html(post.get("category", "agency_prospect"))
    summary = _escape_html(q.get("summary", ""))
    custom_dm = _escape_html(q.get("custom_dm", ""))
    url = post.get("url", "")

    msg = (
        f"🚀 <b>GLOBAL OUTREACH PROSPECT [{today_count}/{DAILY_OUTREACH_TARGET} Today]</b>\n"
        f"🌐 <b>Platform:</b> {platform} | 🎯 <b>Type:</b> {category}\n"
        f"📝 <b>Context:</b> {summary}\n"
        f"🔗 <b>Direct Profile/Post Link:</b> {url}\n\n"
        f"📋 <b>COPY-PASTE PITCH (Includes Live Sample Proof):</b>\n"
        f"<pre>{custom_dm}</pre>"
    )

    if len(msg) > _MAX_MSG_LEN:
        msg = msg[: _MAX_MSG_LEN - 3] + "..."
    return msg


def _format_client_lead_message(post: dict, q: dict) -> str:
    """Build a Telegram-compatible HTML message for SaaS subscriber niches."""
    score = int(q.get("intent_score", 0))
    score_bar = "🟢" * min(score, 10)
    niche = post.get("niche", "")
    niche_label = NICHE_LABELS.get(niche, niche)
    platform = _escape_html(post.get("platform", "Reddit"))

    safe_summary = _escape_html(q.get("summary", ""))
    safe_dm = _escape_html(q.get("custom_dm", ""))
    safe_title = _escape_html(post.get("title", ""))

    msg = (
        f"🚨 <b>New High-Intent Lead ({niche_label})</b>\n\n"
        f"🌐 <b>Platform:</b> {platform} | 📌 <b>Source:</b> {_escape_html(post.get('subreddit', ''))}\n"
        f"⚡ <b>Intent Score:</b> {score}/10  {score_bar}\n\n"
        f"📝 <b>Title:</b> {safe_title}\n\n"
        f"💡 <b>Summary:</b> {safe_summary}\n\n"
        f"🔗 <b>Direct Link:</b> {post.get('url', '')}\n\n"
        f"✉️ <b>Ready-to-Send DM:</b>\n"
        f"<pre>{safe_dm}</pre>"
    )

    if len(msg) > _MAX_MSG_LEN:
        msg = msg[: _MAX_MSG_LEN - 3] + "..."
    return msg


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _send_telegram_message(chat_id: int | str, text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        logger.error("Missing TELEGRAM_BOT_TOKEN or target chat_id.")
        return False

    url = TELEGRAM_API_URL.format(token=TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.exceptions.HTTPError as http_err:
        logger.error("Telegram HTTP error for chat_id=%s: %s — %s", chat_id, http_err, resp.text)
    except requests.exceptions.RequestException as req_err:
        logger.error("Telegram request failed for chat_id=%s: %s", chat_id, req_err)

    return False

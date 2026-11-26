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


def _format_platform_badge(post: dict) -> str:
    """Return a clear emoji + platform + group/subreddit badge for Telegram alerts."""
    platform = (post.get("platform") or "Reddit").strip()
    group = (post.get("group_or_sub") or post.get("subreddit") or "").strip()

    if platform == "Reddit":
        if group:
            sub = group if group.startswith("r/") else f"r/{group}"
            return f"🟠 Reddit ({_escape_html(sub)})"
        return "🟠 Reddit"

    if platform in ("X", "X / Twitter"):
        if group and group.lower() not in ("x", "x / twitter", "x search"):
            return f"𝕏 X / {_escape_html(group.removeprefix('X '))}"
        return "𝕏 X / Community"

    if platform in ("Hacker News", "HackerNews"):
        if group and group.lower() not in ("hacker news", "hackernews"):
            return f"🟧 Hacker News ({_escape_html(group)})"
        return "🟧 Hacker News"

    if platform in ("Indie Hackers", "IndieHackers"):
        if group and group.lower() not in ("indie hackers", "indiehackers"):
            return f"🔵 Indie Hackers ({_escape_html(group)})"
        return "🔵 Indie Hackers"

    if platform == "Product Hunt":
        if group and group.lower() not in ("product hunt", "producthunt"):
            return f"🚀 Product Hunt ({_escape_html(group)})"
        return "🚀 Product Hunt"

    return f"🌐 {_escape_html(platform)}" + (f" ({_escape_html(group)})" if group else "")


def _truncate_snippet(text: str, max_len: int = 360) -> str:
    """Clean whitespace and truncate post description/snippet to ~360 chars."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return "(no description)"
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3].rstrip() + "..."


def _format_admin_outreach_message(post: dict, q: dict, today_count: int) -> str:
    """Format the Global 50/Day Outreach Prospect alert for ADMIN_CHAT_ID."""
    badge = _format_platform_badge(post)
    category = _escape_html(post.get("category", "agency_prospect"))
    score = int(q.get("intent_score", 0))
    safe_title = _escape_html(post.get("title", ""))
    safe_desc = _escape_html(_truncate_snippet(post.get("selftext") or post.get("body") or q.get("summary", "")))
    safe_comment = _escape_html(q.get("suggested_comment") or q.get("custom_dm", ""))
    custom_dm = _escape_html(q.get("custom_dm", ""))
    url = _escape_html(post.get("url", ""))

    msg = (
        f"🚀 <b>GLOBAL OUTREACH PROSPECT [{today_count}/{DAILY_OUTREACH_TARGET} Today]</b>\n"
        f"{badge} | 🎯 <b>Type:</b> {category} | ⚡ <b>Score:</b> {score}/10\n\n"
        f"📝 <b>Title:</b> {safe_title}\n"
        f"📄 <b>Description:</b> {safe_desc}\n\n"
        f"💬 <b>Suggested Public Comment:</b>\n<pre>{safe_comment}</pre>\n\n"
        f"✉️ <b>Suggested Direct Message (DM):</b>\n<pre>{custom_dm}</pre>\n\n"
        f"🔗 <b>Direct Link:</b> <a href=\"{url}\">{url}</a>"
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
    badge = _format_platform_badge(post)

    safe_title = _escape_html(post.get("title", ""))
    raw_desc = post.get("selftext") or post.get("body") or q.get("summary", "")
    safe_desc = _escape_html(_truncate_snippet(raw_desc))
    safe_comment = _escape_html(q.get("suggested_comment") or q.get("custom_dm", ""))
    safe_dm = _escape_html(q.get("custom_dm", ""))
    url = _escape_html(post.get("url", ""))

    msg = (
        f"🚨 <b>New High-Intent Lead ({niche_label})</b>\n\n"
        f"{badge}\n"
        f"⚡ <b>Intent Score:</b> {score}/10  {score_bar}\n\n"
        f"📝 <b>Title:</b> {safe_title}\n"
        f"📄 <b>Description:</b> {safe_desc}\n\n"
        f"💬 <b>Suggested Public Comment:</b>\n"
        f"<pre>{safe_comment}</pre>\n\n"
        f"✉️ <b>Suggested Direct Message (DM):</b>\n"
        f"<pre>{safe_dm}</pre>\n\n"
        f"🔗 <b>Direct Link:</b> <a href=\"{url}\">{url}</a>"
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

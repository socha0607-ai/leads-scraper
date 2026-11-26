"""
ai_filter.py — Gemini 2.5 Flash qualification & pitch generator with sample lead proof.

Supports three categories:
  1. `client_lead`:
     Qualifies real B2B buyers and drafts a helpful 3-sentence outreach DM.
  2. `agency_prospect`:
     Qualifies agency owners / freelancers needing clients, embeds 1-2 live
     verified `client_lead` URLs from `leads.db` as proof of value, and invites
     them to a 5-day free trial on `https://t.me/<bot_username>`.
  3. `distributor_prospect`:
     Qualifies potential B2B SaaS affiliates / growth consultants / partners and
     drafts a 30% recurring lifetime commission ($24/mo per active user) pitch.
"""

import json
import logging
from typing import TypedDict

from google import genai
from google.genai import types

from config import BOT_USERNAME, GEMINI_API_KEY, NICHE_LABELS
from database import get_latest_client_lead_urls

logger = logging.getLogger(__name__)

_client = genai.Client(api_key=GEMINI_API_KEY)
MODEL = "gemini-2.5-flash"


class QualificationResult(TypedDict):
    is_buyer: bool
    intent_score: int
    summary: str
    custom_dm: str


_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_buyer": {
            "type": "boolean",
            "description": (
                "true ONLY if the author fits the target lead/prospect profile "
                "with genuine intent. false for spam or unrelated posts."
            ),
        },
        "intent_score": {
            "type": "integer",
            "description": "1-10 score reflecting urgency, fit, and intent strength.",
        },
        "summary": {
            "type": "string",
            "description": "One-sentence summary of what the person needs or is looking for.",
        },
        "custom_dm": {
            "type": "string",
            "description": "Short, non-spammy, tailored 3-sentence outreach pitch.",
        },
    },
    "required": ["is_buyer", "intent_score", "summary", "custom_dm"],
}

_CLIENT_SYSTEM_PROMPT = (
    "You are an expert B2B sales qualifier helping agencies and freelancers in the "
    "{niche_label} space identify high-intent inbound buyer leads. "
    "Set is_buyer=true ONLY if the author is actively looking to hire or pay for a "
    "service (not a freelancer promoting themselves, not spamming). Draft a helpful, "
    "non-salesy 3-sentence outreach DM tailored to their exact problem. Return ONLY JSON."
)

_AGENCY_PROSPECT_SYSTEM_PROMPT = (
    "You are a B2B SaaS growth strategist for an Intent Lead Scraper Telegram bot "
    "(https://t.me/{bot_username}). Set is_buyer=true ONLY if the author is an agency "
    "owner, dev shop, or freelancer looking for clients or struggling with lead generation. "
    "Draft a 3-sentence non-spammy pitch that: (1) references their exact post/struggle, "
    "(2) embeds 1-2 of these live verified buyer lead URLs as proof of value: {sample_urls}, "
    "and (3) ends with a soft invite to start a 5-day free trial on https://t.me/{bot_username}. "
    "Return ONLY valid JSON."
)

_DISTRIBUTOR_SYSTEM_PROMPT = (
    "You are a SaaS Partnerships Manager for an Intent Lead Scraper Telegram bot "
    "(https://t.me/{bot_username}). Set is_buyer=true ONLY if the author is a B2B affiliate, "
    "micro-SaaS partner, or agency growth consultant open to partnerships or recurring "
    "commissions. Draft a concise, high-value 3-sentence partnership pitch referencing their "
    "post and offering a 30% recurring lifetime commission ($24/mo per active user) to "
    "promote the Intent Lead Scraper bot to their agency/freelancer audience. Return ONLY JSON."
)


def qualify_lead(post: dict, bot_username: str = "") -> QualificationResult | None:
    """Qualify *post* via Gemini 2.5 Flash based on its category and niche."""
    resolved_bot = (BOT_USERNAME or bot_username or "Lead_Scraper01_bot").lstrip("@")
    category = post.get("category", "client_lead")
    niche = post.get("niche", "web_dev")

    if category == "distributor_prospect":
        system_prompt = _DISTRIBUTOR_SYSTEM_PROMPT.format(bot_username=resolved_bot)
    elif category == "agency_prospect" or niche == "admin_growth":
        sample_urls = get_latest_client_lead_urls(limit=3)
        sample_str = ", ".join(sample_urls) if sample_urls else "(no cached sample URLs yet — focus on real-time Reddit/HN buyer alerts)"
        system_prompt = _AGENCY_PROSPECT_SYSTEM_PROMPT.format(
            bot_username=resolved_bot,
            sample_urls=sample_str,
        )
    else:
        niche_label = NICHE_LABELS.get(niche, niche)
        system_prompt = _CLIENT_SYSTEM_PROMPT.format(niche_label=niche_label)

    prompt = _build_prompt(post)

    try:
        response = _client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
                temperature=0.2,
            ),
        )

        raw_text = response.text.strip()
        result: QualificationResult = json.loads(raw_text)

        if not isinstance(result.get("is_buyer"), bool):
            raise ValueError("is_buyer must be a boolean")
        if not (1 <= int(result.get("intent_score", 0)) <= 10):
            raise ValueError("intent_score out of range")

        logger.info(
            "Qualified [%s | %s] %s → is_buyer=%s, score=%s",
            post.get("platform", "Reddit"),
            category,
            post.get("post_id"),
            result["is_buyer"],
            result["intent_score"],
        )
        return result

    except (json.JSONDecodeError, ValueError, KeyError) as parse_err:
        logger.error("Failed to parse Gemini response for %s: %s", post.get("post_id"), parse_err)
        return None
    except Exception as api_err:
        logger.error("Gemini API error for %s: %s", post.get("post_id"), api_err)
        return None


def _build_prompt(post: dict) -> str:
    return (
        f"Platform: {post.get('platform', 'Reddit')}\n"
        f"Category: {post.get('category', 'client_lead')}\n"
        f"Niche: {post.get('niche', 'unknown')}\n"
        f"Source: {post.get('subreddit', 'unknown')}\n"
        f"Title: {post.get('title', '')}\n"
        f"Body:\n{post.get('selftext', post.get('body', '(no body)'))}\n\n"
        "Analyse the above post and return a JSON object matching the schema."
    )

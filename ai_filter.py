"""
ai_filter.py — Gemini qualification & pitch generator with automatic model fallback
and rule-based buyer-intent safety net.
"""

import json
import logging
from typing import TypedDict

from google import genai
from google.genai import types

from config import BOT_USERNAME, GEMINI_API_KEY, GEMINI_MODEL, NICHE_LABELS
from database import get_latest_client_lead_urls

logger = logging.getLogger(__name__)

_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
MODEL = GEMINI_MODEL
_FALLBACK_MODELS: list[str] = [
    "gemini-3.8-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
]
_ACTIVE_MODEL: str | None = None

_BUYER_INTENT_PHRASES: tuple[str, ...] = (
    "looking for",
    "need",
    "hire",
    "recommend",
    "help with",
    "agency",
    "freelancer",
    "how do i",
    "struggling",
)


class QualificationResult(TypedDict):
    is_buyer: bool
    intent_score: int
    summary: str
    suggested_comment: str
    custom_dm: str


_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_buyer": {
            "type": "boolean",
            "description": (
                "true ONLY if the author is genuinely asking for help, recommendations, "
                "or services. false for technical news, research papers (e.g. LLM papers "
                "mentioning 'audit'), self-promotion, or unrelated posts."
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
        "suggested_comment": {
            "type": "string",
            "description": (
                "Conversational, warm, genuinely helpful 2-3 sentence public comment "
                "referencing a specific detail from their post. Zero sales pitch or "
                "buzzwords; sounds like a peer founder or engineer helping out."
            ),
        },
        "custom_dm": {
            "type": "string",
            "description": (
                "Casual, personalized 2-sentence direct message referencing their exact "
                "project or problem and offering one specific helpful tip. Never automated or spammy."
            ),
        },
    },
    "required": ["is_buyer", "intent_score", "summary", "suggested_comment", "custom_dm"],
}

_CLIENT_SYSTEM_PROMPT = (
    "You are an expert B2B lead qualifier helping founders, engineers, and consultants in the "
    "{niche_label} space spot real people asking for help across Reddit, X, Hacker News, "
    "Indie Hackers, and Product Hunt.\n"
    "STRICT QUALIFICATION: Set is_buyer=true ONLY when the post shows someone genuinely asking "
    "for help, recommendations, feedback, or services. Set is_buyer=false for random technical "
    "news, research papers (like sandboxed LLM papers that merely contain 'audit'), product announcements "
    "with no help request, or freelancers promoting themselves.\n"
    "REPLY STYLE: Read the actual title and description carefully and write:\n"
    "1. `suggested_comment`: Conversational, warm, genuinely helpful (2-3 sentences max), "
    "referencing a specific detail from their post, zero sales pitch or buzzwords, sounding like a peer founder/engineer helping out.\n"
    "2. `custom_dm`: Casual, personalized 2-sentence outreach referencing their exact project/problem "
    "and offering one specific helpful tip — never sounding automated or spammy.\n"
    "Return ONLY valid JSON."
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


def _candidate_models() -> list[str]:
    """Return deduplicated list of Gemini models starting with active/configured model."""
    ordered = ([_ACTIVE_MODEL] if _ACTIVE_MODEL else []) + [MODEL] + _FALLBACK_MODELS
    seen: set[str] = set()
    result: list[str] = []
    for m in ordered:
        if m and m not in seen:
            seen.add(m)
            result.append(m)
    return result


def _is_not_found_error(exc: Exception) -> bool:
    msg = str(exc).upper()
    return "404" in msg or "NOT_FOUND" in msg or "NOT FOUND" in msg


def _discover_flash_model(exclude: set[str]) -> str | None:
    """Dynamically pick an available `generateContent` flash model via models.list()."""
    if not _client:
        return None
    try:
        for m in _client.models.list():
            name = (getattr(m, "name", "") or "").removeprefix("models/")
            actions = getattr(m, "supported_actions", None) or []
            if (
                name
                and name not in exclude
                and "flash" in name.lower()
                and (not actions or "generateContent" in actions)
            ):
                logger.info("Discovered available Gemini flash model: %s", name)
                return name
    except Exception as exc:
        logger.debug("Could not list Gemini models: %s", exc)
    return None


def _rule_based_fallback(post: dict) -> QualificationResult:
    """
    Safety net when Gemini API fails (404, 429, timeout, or missing key):
    never silently drop candidates; check buyer-intent phrases and assign 7/10.
    """
    title = (post.get("title") or "").strip()
    body = (post.get("selftext") or post.get("body") or "").strip()
    combined = f"{title} {body}".lower()
    matched = next((p for p in _BUYER_INTENT_PHRASES if p in combined), None)

    if not matched:
        return {
            "is_buyer": False,
            "intent_score": 1,
            "summary": "No clear buyer-intent phrase matched (rule-based fallback).",
            "suggested_comment": "",
            "custom_dm": "",
        }

    short_title = title[:90] if title else "your post"
    return {
        "is_buyer": True,
        "intent_score": 7,
        "summary": f"Matched buyer-intent phrase '{matched}' in: {short_title}",
        "suggested_comment": (
            f"Saw your post about \"{short_title}\" — happy to share a quick breakdown of what "
            f"usually moves the needle first here so you don't waste time or budget. Feel free to "
            f"share a bit more about your current setup if you'd like a second pair of eyes."
        ),
        "custom_dm": (
            f"Hey! Saw your post on \"{short_title}\" and wanted to reach out with a quick tip "
            f"on tackling the first bottleneck. Happy to send over a 2-minute teardown if helpful!"
        ),
    }


def _call_gemini(model_name: str, system_prompt: str, prompt: str) -> QualificationResult:
    assert _client is not None
    response = _client.models.generate_content(
        model=model_name,
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
    return result


def qualify_lead(post: dict, bot_username: str = "") -> QualificationResult:
    """
    Qualify *post* via Gemini with automatic model fallback (`gemini-3.8-flash` ->
    `gemini-2.5-pro` -> `gemini-2.0-flash` -> dynamic `models.list()`).
    Falls back to rule-based buyer-intent scoring (7/10) on API errors so leads
    are never silently dropped.
    """
    global _ACTIVE_MODEL

    resolved_bot = (BOT_USERNAME or bot_username or "Lead_Scraper01_bot").lstrip("@")
    category = post.get("category", "client_lead")
    niche = post.get("niche", "web_dev")

    if category == "distributor_prospect":
        system_prompt = _DISTRIBUTOR_SYSTEM_PROMPT.format(bot_username=resolved_bot)
    elif category == "agency_prospect" or niche == "admin_growth":
        sample_urls = get_latest_client_lead_urls(limit=3)
        sample_str = (
            ", ".join(sample_urls)
            if sample_urls
            else "(no cached sample URLs yet — focus on real-time Reddit/HN buyer alerts)"
        )
        system_prompt = _AGENCY_PROSPECT_SYSTEM_PROMPT.format(
            bot_username=resolved_bot,
            sample_urls=sample_str,
        )
    else:
        niche_label = NICHE_LABELS.get(niche, niche)
        system_prompt = _CLIENT_SYSTEM_PROMPT.format(niche_label=niche_label)

    if not _client:
        logger.warning("GEMINI_API_KEY not set — using rule-based fallback for %s", post.get("post_id"))
        return _rule_based_fallback(post)

    prompt = _build_prompt(post)
    tried: set[str] = set()
    last_err: Exception | None = None

    for model_name in _candidate_models():
        tried.add(model_name)
        try:
            result = _call_gemini(model_name, system_prompt, prompt)
            _ACTIVE_MODEL = model_name
            logger.info(
                "Qualified [%s | %s | %s] %s → is_buyer=%s, score=%s",
                post.get("platform", "Reddit"),
                category,
                model_name,
                post.get("post_id"),
                result["is_buyer"],
                result["intent_score"],
            )
            return result
        except Exception as err:
            last_err = err
            if _is_not_found_error(err):
                logger.warning("Gemini model '%s' returned 404/NOT_FOUND — trying next fallback.", model_name)
                continue
            logger.warning("Gemini error on '%s' for %s: %s", model_name, post.get("post_id"), err)
            break

    if last_err and _is_not_found_error(last_err):
        discovered = _discover_flash_model(tried)
        if discovered:
            try:
                result = _call_gemini(discovered, system_prompt, prompt)
                _ACTIVE_MODEL = discovered
                logger.info(
                    "Qualified [%s | %s | %s] %s → is_buyer=%s, score=%s",
                    post.get("platform", "Reddit"),
                    category,
                    discovered,
                    post.get("post_id"),
                    result["is_buyer"],
                    result["intent_score"],
                )
                return result
            except Exception as err:
                logger.warning("Discovered model '%s' failed for %s: %s", discovered, post.get("post_id"), err)

    logger.warning(
        "Gemini unavailable for %s (%s) — using rule-based buyer-intent fallback.",
        post.get("post_id"),
        last_err,
    )
    return _rule_based_fallback(post)


def _build_prompt(post: dict) -> str:
    group_or_sub = post.get("group_or_sub") or post.get("subreddit") or "unknown"
    body = post.get("selftext") or post.get("body") or "(no description)"
    return (
        f"Platform: {post.get('platform', 'Reddit')}\n"
        f"Group/Source: {group_or_sub}\n"
        f"Author: {post.get('author') or 'unknown'}\n"
        f"Category: {post.get('category', 'client_lead')}\n"
        f"Niche: {post.get('niche', 'unknown')}\n"
        f"Title: {post.get('title', '')}\n"
        f"Description:\n{body}\n\n"
        "Analyse the above post carefully. Only set is_buyer=true if the author is genuinely "
        "asking for help, recommendations, or services. Return a JSON object matching the schema."
    )

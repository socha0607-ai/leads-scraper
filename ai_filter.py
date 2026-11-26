"""
ai_filter.py — Single 50-lead batch AI qualifier & human-style pitch generator.

Uses Groq API (`llama-3.3-70b-versatile`) as ultra-fast primary and falls back
automatically to Gemini (`gemini-3.8-flash` -> `gemini-3.1-pro-preview`), with
a local buyer-signal safety net so leads are never dropped silently.
"""

import json
import logging
import os
import time
from typing import TypedDict

import requests
from google import genai
from google.genai import types

from config import (
    GEMINI_API_KEY,
    GEMINI_FALLBACK_MODELS,
    GEMINI_MODEL,
    GROQ_API_KEY,
    GROQ_API_URL,
    GROQ_MODEL,
    MIN_INTENT_SCORE,
)
from database import is_post_processed

logger = logging.getLogger(__name__)

MAX_BATCH_CANDIDATES = 50
AI_TEMPERATURE = 0.85

MODEL = GEMINI_MODEL
_FALLBACK_MODELS: list[str] = list(GEMINI_FALLBACK_MODELS)
_ACTIVE_MODEL: str | None = None
_GEMINI_CLIENT: genai.Client | None = None
_GEMINI_CLIENT_KEY: str = ""

_PREFILTER_SIGNALS: tuple[str, ...] = (
    "?",
    "need",
    "looking",
    "help",
    "recommend",
    "suggest",
    "hire",
    "agency",
    "freelancer",
    "best",
    "how to",
    "how do i",
    "struggling",
    "client",
    "traffic",
    "ads",
    "seo",
    "build",
    "automate",
    "anyone",
    "advice",
)

_BUYER_INTENT_PHRASES: tuple[str, ...] = (
    "?",
    "looking for",
    "need",
    "hire",
    "recommend",
    "help with",
    "agency",
    "freelancer",
    "how do i",
    "how to",
    "struggling",
)


class QualificationResult(TypedDict):
    is_buyer: bool
    intent_score: int
    summary: str
    suggested_comment: str
    custom_dm: str


_BATCH_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "leads": {
            "type": "array",
            "description": "ONLY high-intent buyer posts with intent_score >= 7.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Exact post id from the input batch.",
                    },
                    "intent_score": {
                        "type": "integer",
                        "description": "7-10 score reflecting urgency, buyer intent, and fit.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "One concise sentence summarizing what the author specifically needs.",
                    },
                    "public_comment": {
                        "type": "string",
                        "description": (
                            "Unique, conversational, peer-to-peer 2-3 sentence comment referencing a specific "
                            "detail from this post. Zero sales pitch and zero banned clichés."
                        ),
                    },
                    "direct_message": {
                        "type": "string",
                        "description": (
                            "Tailored 2-sentence DM referencing specific details from their post with one "
                            "concrete helpful tip, sounding natural and unscripted."
                        ),
                    },
                },
                "required": ["id", "intent_score", "summary", "public_comment", "direct_message"],
            },
        }
    },
    "required": ["leads"],
}

_BATCH_SYSTEM_PROMPT = (
    "You are an expert B2B buyer-intent qualifier and founder-to-founder copywriter. "
    "You will receive up to 50 social posts (id, platform, niche, title, description).\n\n"
    "Selection rules:\n"
    "- Return ONLY genuine high-intent buyer posts (intent_score >= 7) where the author is asking "
    "for help, recommendations, audits, developers, agencies, or automation solutions.\n"
    "- Exclude self-promotion, 'for hire' ads, news articles, and generic announcements.\n\n"
    "Strict human-style writing rules for every qualified lead:\n"
    "- `public_comment`: Write a completely unique, conversational, peer-to-peer 2-3 sentence reply "
    "referencing a specific concrete detail from THAT post. Zero sales pitch, zero links, zero self-promotion.\n"
    "- `direct_message`: Write a tailored 2-sentence DM referencing exact details from their post and offering "
    "one specific helpful observation or quick tip.\n"
    "- BANNED CLICHÉS (NEVER use these anywhere): 'Great question', 'Hey! Saw your post', 'Saw your post', "
    "'I'd love to help', 'Feel free to reach out', 'Let me know if you need anything', 'As an expert'. "
    "Vary your opening and phrasing across every single post.\n\n"
    "Respond ONLY with valid JSON shaped as: {\"leads\": [{\"id\": ..., \"intent_score\": ..., "
    "\"summary\": ..., \"public_comment\": ..., \"direct_message\": ...}]}."
)


def _get_gemini_client() -> genai.Client | None:
    """Return a cached Gemini client using env or config GEMINI_API_KEY."""
    global _GEMINI_CLIENT, _GEMINI_CLIENT_KEY
    api_key = (os.getenv("GEMINI_API_KEY") or GEMINI_API_KEY or "").strip()
    if not api_key:
        return None
    if _GEMINI_CLIENT is None or _GEMINI_CLIENT_KEY != api_key:
        _GEMINI_CLIENT = genai.Client(api_key=api_key)
        _GEMINI_CLIENT_KEY = api_key
    return _GEMINI_CLIENT


def _get_groq_key() -> str:
    return (os.getenv("GROQ_API_KEY") or GROQ_API_KEY or "").strip()


def prefilter_candidates(
    posts: list[dict],
    active_niches: set[str] | list[str] | None = None,
    max_candidates: int = MAX_BATCH_CANDIDATES,
) -> list[dict]:
    """
    Filter scraped posts locally before spending any AI call:
      1. Keep ONLY unseen posts (`not is_post_processed(post_id)`)
      2. Keep ONLY posts matching `active_niches` (when provided)
      3. Keep ONLY posts showing buyer/question intent signals
      4. Sort by signal density & freshness and cap to `max_candidates` (default 50)
    """
    if active_niches is not None and not active_niches:
        return []

    niche_filter = set(active_niches) if active_niches is not None else None
    seen_ids: set[str] = set()
    scored: list[tuple[int, int, dict]] = []

    for idx, post in enumerate(posts):
        post_id = str(post.get("post_id") or "").strip()
        if not post_id or post_id in seen_ids:
            continue

        if niche_filter is not None and post.get("niche") not in niche_filter:
            continue

        if is_post_processed(post_id):
            continue

        title = (post.get("title") or "").lower()
        body = (post.get("selftext") or post.get("body") or "").lower()
        combined = f"{title} {body}"

        signal_hits = sum(1 for sig in _PREFILTER_SIGNALS if sig in combined)
        if signal_hits <= 0:
            continue

        seen_ids.add(post_id)
        scored.append((signal_hits, -idx, post))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    selected = [item[2] for item in scored[:max_candidates]]
    logger.info(
        "Pre-filtered %d post(s) -> %d high-signal unseen candidate(s) (cap %d).",
        len(posts),
        len(selected),
        max_candidates,
    )
    return selected


def _candidate_models() -> list[str]:
    primary_env = (os.getenv("GEMINI_MODEL") or MODEL or "").strip()
    ordered = ([_ACTIVE_MODEL] if _ACTIVE_MODEL else []) + [primary_env] + _FALLBACK_MODELS
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


def _is_transient_error(exc: Exception) -> bool:
    msg = str(exc).upper()
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg or "UNAVAILABLE" in msg


def _discover_flash_model(exclude: set[str]) -> str | None:
    client = _get_gemini_client()
    if not client:
        return None
    try:
        for m in client.models.list():
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
    """Local buyer-signal fallback (score 7/10) when Groq and Gemini are unavailable."""
    title = (post.get("title") or "").strip()
    body = (post.get("selftext") or post.get("body") or "").strip()
    combined = f"{title} {body}".lower()
    matched = next((p for p in _BUYER_INTENT_PHRASES if p in combined), None)

    if not matched:
        return {
            "is_buyer": False,
            "intent_score": 1,
            "summary": "No clear buyer-intent signal matched (local fallback).",
            "suggested_comment": "",
            "custom_dm": "",
        }

    short_topic = " ".join(title.split())[:80] or "this bottleneck"
    return {
        "is_buyer": True,
        "intent_score": max(MIN_INTENT_SCORE, 7),
        "summary": f"Matched buyer signal ('{matched}') in: {short_topic}",
        "suggested_comment": (
            f"Dealing with {short_topic} usually comes down to auditing where intent leaks first "
            f"before adding extra tools or spend. Tackling the core workflow step by step saves a lot of guesswork."
        ),
        "custom_dm": (
            f"Read your note on {short_topic} — one quick win is isolating the top bottleneck before changing the whole setup. "
            f"Happy to send over a 2-bullet checklist based on what usually resolves this fastest."
        ),
    }


def _batch_rule_based_fallback(candidates: list[dict]) -> dict[str, QualificationResult]:
    """Qualify a batch locally when both Groq and Gemini are unavailable."""
    results: dict[str, QualificationResult] = {}
    for post in candidates:
        post_id = str(post.get("post_id") or "")
        if not post_id:
            continue
        res = _rule_based_fallback(post)
        if res["is_buyer"]:
            results[post_id] = res
    logger.info("Local buyer-signal fallback qualified %d/%d candidate(s).", len(results), len(candidates))
    return results


def _build_batch_prompt(candidates: list[dict]) -> str:
    compact_items = []
    for post in candidates:
        desc = " ".join((post.get("selftext") or post.get("body") or "").split())[:220]
        compact_items.append(
            {
                "id": str(post.get("post_id") or ""),
                "platform": post.get("platform", "Reddit"),
                "niche": post.get("niche", ""),
                "title": (post.get("title") or "")[:160],
                "description": desc,
            }
        )
    return (
        "Evaluate these candidate posts and return JSON containing ONLY high-intent buyer posts "
        "(intent_score >= 7):\n\n"
        + json.dumps(compact_items, ensure_ascii=False)
    )


def _extract_items_from_json(raw_text: str) -> list[dict]:
    """Parse AI JSON response supporting both `{"leads": [...]}` and bare `[...]` arrays."""
    cleaned = (raw_text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()

    parsed = json.loads(cleaned)
    if isinstance(parsed, list):
        return [x for x in parsed if isinstance(x, dict)]
    if isinstance(parsed, dict):
        for key in ("leads", "items", "results", "data"):
            val = parsed.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        for val in parsed.values():
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    raise ValueError("Expected JSON object with 'leads' list from AI batch qualification")


def _call_groq_batch(prompt: str, api_key: str) -> list[dict]:
    """Call Groq OpenAI-compatible chat completions endpoint with temperature=0.85."""
    model = (os.getenv("GROQ_MODEL") or GROQ_MODEL).strip() or "llama-3.3-70b-versatile"
    resp = requests.post(
        GROQ_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "temperature": AI_TEMPERATURE,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _BATCH_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return _extract_items_from_json(content)


def _call_gemini_batch(model_name: str, prompt: str) -> list[dict]:
    client = _get_gemini_client()
    assert client is not None
    response = client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_BATCH_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=_BATCH_RESPONSE_SCHEMA,
            temperature=AI_TEMPERATURE,
        ),
    )
    return _extract_items_from_json(response.text)


def _qualify_with_gemini_fallback(candidates: list[dict], prompt: str) -> list[dict] | None:
    """Try Gemini model chain (gemini-3.8-flash -> gemini-3.1-pro-preview -> backups)."""
    global _ACTIVE_MODEL
    client = _get_gemini_client()
    if not client:
        return None

    tried: set[str] = set()
    last_err: Exception | None = None
    raw_items: list[dict] | None = None

    for model_name in _candidate_models():
        tried.add(model_name)
        try:
            raw_items = _call_gemini_batch(model_name, prompt)
            _ACTIVE_MODEL = model_name
            logger.info(
                "Batch-qualified %d candidate(s) via Gemini (%s) -> %d buyer(s).",
                len(candidates),
                model_name,
                len(raw_items),
            )
            return raw_items
        except Exception as err:
            last_err = err
            if _is_transient_error(err):
                next_model = next(
                    (m for m in ("gemini-3.1-pro-preview", "gemini-2.5-pro", "gemini-2.0-flash") if m != model_name),
                    "gemini-3.1-pro-preview",
                )
                logger.warning(
                    "Gemini transient error (503/429) on '%s' — sleeping 5s and retrying once with '%s'…",
                    model_name,
                    next_model,
                )
                time.sleep(5)
                try:
                    raw_items = _call_gemini_batch(next_model, prompt)
                    _ACTIVE_MODEL = next_model
                    logger.info(
                        "Gemini retry succeeded via %s -> %d buyer(s).",
                        next_model,
                        len(raw_items),
                    )
                    return raw_items
                except Exception as retry_err:
                    last_err = retry_err
                    logger.warning("Gemini retry failed on '%s': %s", next_model, retry_err)
                    return None
            if _is_not_found_error(err):
                logger.warning("Gemini model '%s' returned 404/NOT_FOUND — trying next fallback.", model_name)
                continue
            logger.warning("Gemini batch error on '%s': %s", model_name, err)
            return None

    if last_err and _is_not_found_error(last_err):
        discovered = _discover_flash_model(tried)
        if discovered:
            try:
                raw_items = _call_gemini_batch(discovered, prompt)
                _ACTIVE_MODEL = discovered
                return raw_items
            except Exception as err:
                logger.warning("Discovered Gemini model '%s' failed: %s", discovered, err)

    return None


def qualify_leads_batch(
    posts: list[dict],
    bot_username: str = "",
    active_niches: set[str] | list[str] | None = None,
    already_prefiltered: bool = False,
) -> dict[str, QualificationResult]:
    """
    Pre-filter *posts* (up to 50) locally, then qualify them in ONE single AI batch request:
      1. Groq primary (`llama-3.3-70b-versatile`) when `GROQ_API_KEY` is configured
      2. Gemini backup (`gemini-3.8-flash` -> `gemini-3.1-pro-preview`) if Groq absent/fails
      3. Local buyer-signal safety net if both providers are unavailable
    """
    del bot_username  # preserved for signature compatibility

    candidates = (
        list(posts[:MAX_BATCH_CANDIDATES])
        if already_prefiltered
        else prefilter_candidates(
            posts,
            active_niches=active_niches,
            max_candidates=MAX_BATCH_CANDIDATES,
        )
    )
    if not candidates:
        return {}

    prompt = _build_batch_prompt(candidates)
    raw_items: list[dict] | None = None

    groq_key = _get_groq_key()
    if groq_key:
        try:
            raw_items = _call_groq_batch(prompt, groq_key)
            logger.info(
                "Batch-qualified %d candidate(s) via Groq (%s) -> %d buyer(s).",
                len(candidates),
                (os.getenv("GROQ_MODEL") or GROQ_MODEL),
                len(raw_items),
            )
        except Exception as exc:
            logger.warning("Groq batch request failed (%s) — falling back to Gemini…", exc)

    if raw_items is None:
        raw_items = _qualify_with_gemini_fallback(candidates, prompt)

    if raw_items is None:
        logger.warning("Both Groq and Gemini unavailable — using local buyer-signal fallback.")
        return _batch_rule_based_fallback(candidates)

    min_score = max(MIN_INTENT_SCORE, 7)
    results: dict[str, QualificationResult] = {}
    for item in raw_items:
        pid = str(item.get("id") or "").strip()
        try:
            score = int(item.get("intent_score") or 0)
        except (TypeError, ValueError):
            score = 0
        if not pid or score < min_score:
            continue
        results[pid] = {
            "is_buyer": True,
            "intent_score": min(score, 10),
            "summary": str(item.get("summary") or "").strip(),
            "suggested_comment": str(item.get("public_comment") or "").strip(),
            "custom_dm": str(item.get("direct_message") or "").strip(),
        }

    return results


def qualify_lead(post: dict, bot_username: str = "") -> QualificationResult:
    """Single-post wrapper around `qualify_leads_batch` (used by `/test_scrape`)."""
    temp_post = dict(post)
    pid = str(temp_post.get("post_id") or "single_test_post")
    temp_post["post_id"] = pid
    batch = qualify_leads_batch([temp_post], bot_username=bot_username)
    if pid in batch:
        return batch[pid]
    return {
        "is_buyer": False,
        "intent_score": 1,
        "summary": "Did not meet >= 7/10 buyer-intent threshold or lacked buyer intent signals.",
        "suggested_comment": "",
        "custom_dm": "",
    }

"""
ai_filter.py — Batched Gemini qualification & pitch generator with pre-filtering,
429 rate-limit backoff, automatic model fallback, and rule-based safety net.
"""

import json
import logging
import time
from typing import TypedDict

from google import genai
from google.genai import types

from config import BOT_USERNAME, GEMINI_API_KEY, GEMINI_MODEL

logger = logging.getLogger(__name__)

_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
MODEL = GEMINI_MODEL
_FALLBACK_MODELS: list[str] = [
    "gemini-3.8-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
]
_ACTIVE_MODEL: str | None = None

_PREFILTER_SIGNALS: tuple[str, ...] = (
    "?",
    "need",
    "looking",
    "help",
    "recommend",
    "hire",
    "agency",
    "freelancer",
    "best",
    "how to",
    "struggling",
    "client",
    "traffic",
    "ads",
    "seo",
    "build",
    "automate",
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
    "type": "array",
    "description": "ONLY high-intent buyer posts with score >= 7.",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Exact post id from the input batch."},
            "intent_score": {
                "type": "integer",
                "description": "7-10 score reflecting urgency, fit, and buyer intent.",
            },
            "summary": {
                "type": "string",
                "description": "One-sentence summary of what the author needs.",
            },
            "public_comment": {
                "type": "string",
                "description": (
                    "Conversational, warm, genuinely helpful 2-3 sentence public reply "
                    "referencing a specific detail from their post. Zero sales pitch."
                ),
            },
            "direct_message": {
                "type": "string",
                "description": (
                    "Casual, personalized 2-sentence DM referencing their exact project "
                    "or problem and offering one specific helpful tip."
                ),
            },
        },
        "required": ["id", "intent_score", "summary", "public_comment", "direct_message"],
    },
}

_BATCH_SYSTEM_PROMPT = (
    "You are an expert B2B lead qualifier. You will receive up to 15 social posts "
    "(id, platform, title, description). "
    "Return a JSON array containing ONLY the high-intent buyer posts (intent_score >= 7) "
    "where someone is genuinely asking for help, recommendations, or services. "
    "Exclude random technical news, research papers, or self-promotion. "
    "For each qualified post, write a warm, peer-style `public_comment` (2-3 sentences, zero sales pitch) "
    "and a casual, personalized `direct_message` (2 sentences, referencing their exact problem). "
    "Return ONLY valid JSON."
)


def prefilter_candidates(posts: list[dict], max_candidates: int = 15) -> list[dict]:
    """
    Deduplicate candidates by `post_id`, prioritize posts containing buyer/question
    signals, and cap the batch to the top *max_candidates* freshest unseen posts.
    """
    seen_ids: set[str] = set()
    scored: list[tuple[int, int, dict]] = []

    for idx, post in enumerate(posts):
        post_id = str(post.get("post_id") or "")
        if not post_id or post_id in seen_ids:
            continue
        seen_ids.add(post_id)

        title = (post.get("title") or "").lower()
        body = (post.get("selftext") or post.get("body") or "").lower()
        combined = f"{title} {body}"

        signal_hits = sum(1 for sig in _PREFILTER_SIGNALS if sig in combined)
        # Prioritize signal-rich posts while preserving original freshness order on ties
        scored.append((signal_hits, -idx, post))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    selected = [item[2] for item in scored[:max_candidates]]
    logger.info(
        "Pre-filtered %d candidate(s) down to top %d for single-batch Gemini call.",
        len(seen_ids),
        len(selected),
    )
    return selected


def _candidate_models() -> list[str]:
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


def _is_transient_error(exc: Exception) -> bool:
    msg = str(exc).upper()
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg or "UNAVAILABLE" in msg


def _discover_flash_model(exclude: set[str]) -> str | None:
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
    """Local buyer-signal fallback (score 7/10) when Gemini is unavailable."""
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

    short_title = title[:90] if title else "your post"
    return {
        "is_buyer": True,
        "intent_score": 7,
        "summary": f"Matched buyer signal ('{matched}') in: {short_title}",
        "suggested_comment": (
            f"Saw your post on \"{short_title}\" — usually the fastest win here is auditing "
            f"the core bottleneck before spending more time or budget. Happy to share a quick "
            f"breakdown if a second pair of eyes would help."
        ),
        "custom_dm": (
            f"Hey! Saw your post about \"{short_title}\" and thought I'd share a quick pointer "
            f"on what tends to work best first. Happy to send over a 2-minute teardown if helpful."
        ),
    }


def _batch_rule_based_fallback(candidates: list[dict]) -> dict[str, QualificationResult]:
    """Qualify a batch locally when Gemini is rate-limited or unreachable."""
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
                "title": (post.get("title") or "")[:160],
                "description": desc,
            }
        )
    return (
        "Evaluate these candidate posts and return ONLY the high-intent buyer posts "
        "(intent_score >= 7) as a JSON array:\n\n"
        + json.dumps(compact_items, ensure_ascii=False)
    )


def _call_gemini_batch(model_name: str, prompt: str) -> list[dict]:
    assert _client is not None
    response = _client.models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=_BATCH_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=_BATCH_RESPONSE_SCHEMA,
            temperature=0.2,
        ),
    )
    parsed = json.loads(response.text.strip())
    if not isinstance(parsed, list):
        raise ValueError("Expected JSON array from batch qualification")
    return parsed


def qualify_leads_batch(posts: list[dict], bot_username: str = "") -> dict[str, QualificationResult]:
    """
    Pre-filter *posts* (max 15) and qualify them in ONE single Gemini API call.
    Handles 404 model fallback, 429 rate-limit backoff (12s + retry once), and
    local buyer-signal fallback so leads are never dropped.
    """
    global _ACTIVE_MODEL
    del bot_username  # kept for signature compatibility

    candidates = prefilter_candidates(posts, max_candidates=15)
    if not candidates:
        return {}

    if not _client:
        logger.warning("GEMINI_API_KEY not set — using local buyer-signal fallback.")
        return _batch_rule_based_fallback(candidates)

    prompt = _build_batch_prompt(candidates)
    tried: set[str] = set()
    last_err: Exception | None = None
    raw_items: list[dict] | None = None

    for model_name in _candidate_models():
        tried.add(model_name)
        try:
            result = _call_gemini_batch(model_name, prompt)
            _ACTIVE_MODEL = model_name
            logger.info("Batch-qualified %d candidates via %s -> %d buyer(s).", len(candidates), model_name, len(raw_items) if (raw_items := result) is not None else 0)
            break
        except Exception as err:
            last_err = err
            if _is_transient_error(err):
                fallback_model = next((m for m in ("gemini-2.5-pro", "gemini-2.0-flash") if m != model_name), "gemini-2.0-flash")
                logger.warning(
                    "Gemini transient error (503/429) on '%s' — sleeping 5s and retrying once with '%s'…",
                    model_name,
                    fallback_model,
                )
                time.sleep(5)
                try:
                    raw_items = _call_gemini_batch(fallback_model, prompt)
                    _ACTIVE_MODEL = fallback_model
                    logger.info("Batch retry succeeded via %s -> %d buyer(s).", fallback_model, len(raw_items))
                    break
                except Exception as retry_err:
                    last_err = retry_err
                    logger.warning("Gemini retry failed on '%s': %s", fallback_model, retry_err)
                    break
            if _is_not_found_error(err):
                logger.warning("Gemini model '%s' returned 404/NOT_FOUND — trying next fallback.", model_name)
                continue
            logger.warning("Gemini batch error on '%s': %s", model_name, err)
            break

    if raw_items is None and last_err and _is_not_found_error(last_err):
        discovered = _discover_flash_model(tried)
        if discovered:
            try:
                raw_items = _call_gemini_batch(discovered, prompt)
                _ACTIVE_MODEL = discovered
            except Exception as err:
                last_err = err

    if raw_items is None:
        logger.warning("Gemini batch unavailable (%s) — using local buyer-signal fallback.", last_err)
        return _batch_rule_based_fallback(candidates)

    results: dict[str, QualificationResult] = {}
    for item in raw_items:
        pid = str(item.get("id") or "").strip()
        score = int(item.get("intent_score") or 0)
        if not pid or score < 7:
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
        "summary": "Did not meet >= 7/10 buyer-intent threshold.",
        "suggested_comment": "",
        "custom_dm": "",
    }

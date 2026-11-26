"""
scraper.py — Multi-Platform Global Harvester (Reddit, Hacker News, X / Twitter, IndieHackers).

Combines three scraping engines into `scrape_all_platforms()`:
  1. `scrape_reddit_global(page)`      — Playwright on old.reddit.com/search + subreddit /new/
  2. `scrape_hackernews_api()`         — Fast JSON API via hn.algolia.com (last 24h)
  3. `scrape_web_social_global(page)`  — Playwright on DuckDuckGo HTML for X & IndieHackers

Every item is deduplicated against `leads.db` and tagged with:
  - `platform`: "Reddit" | "HackerNews" | "X / Twitter" | "IndieHackers"
  - `category`: "client_lead" | "agency_prospect" | "distributor_prospect"
"""

import hashlib
import logging
import re
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
from playwright.sync_api import Browser, Page, TimeoutError as PWTimeout, sync_playwright

from config import (
    ADMIN_GLOBAL_QUERIES,
    DDG_HTML_URL,
    DISTRIBUTOR_QUERIES,
    HN_API_URL,
    NICHES,
    USER_AGENT,
    WEB_SOCIAL_QUERIES,
)
from database import is_post_processed, mark_post_processed

logger = logging.getLogger(__name__)

OLD_REDDIT_NEW_URL = "https://old.reddit.com/r/{sub}/new/"
OLD_REDDIT_SEARCH_URL = "https://old.reddit.com/search?q={query}&sort=new&t=day"
WWW_REDDIT_NEW_URL = "https://www.reddit.com/r/{sub}/new/"
WWW_REDDIT_SEARCH_URL = "https://www.reddit.com/search/?q={query}&type=posts&sort=new&t=day"
OLD_REDDIT_BASE = "https://old.reddit.com"
REDDIT_BASE = "https://www.reddit.com"

MAX_POSTS_PER_SUB = 20
MAX_SEARCH_RESULTS = 15
NAV_TIMEOUT_MS = 25_000
SETTLE_WAIT_MS = 2_000

_COMMENT_ID_RE = re.compile(r"/comments/([a-z0-9]+)/", re.IGNORECASE)
_SUBREDDIT_RE = re.compile(r"/r/([^/]+)/", re.IGNORECASE)


def scrape_all_platforms() -> list[dict]:
    """
    Run all three global harvesters (Hacker News API, Reddit Global, Web/Social)
    and return deduplicated lead dicts ready for AI qualification.
    """
    leads: list[dict] = []

    # 1. Fast JSON API: Hacker News (no browser needed)
    try:
        hn_leads = scrape_hackernews_api()
        leads.extend(hn_leads)
    except Exception as exc:
        logger.warning("Hacker News scraper failed: %s", exc)

    # 2. Browser-based harvesters: Reddit Global + DuckDuckGo (X / IndieHackers)
    with sync_playwright() as pw:
        browser: Browser = _launch_browser(pw)
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            page: Page = context.new_page()

            try:
                leads.extend(scrape_reddit_global(page))
            except Exception as exc:
                logger.warning("Reddit global scraper failed: %s", exc)

            try:
                leads.extend(scrape_web_social_global(page))
            except Exception as exc:
                logger.warning("Web/social scraper failed: %s", exc)
        finally:
            browser.close()

    logger.info("Multi-platform harvest complete. %d new lead(s) queued for AI.", len(leads))
    return leads


# Backward-compatible alias
scrape_subreddits = scrape_all_platforms


def _launch_browser(pw) -> Browser:
    """Launch Chromium, falling back to any cached headless shell if needed."""
    try:
        return pw.chromium.launch(headless=True)
    except Exception as exc:
        cache_dir = Path.home() / ".cache" / "ms-playwright"
        candidates = sorted(
            cache_dir.glob("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"),
            reverse=True,
        )
        if candidates:
            logger.info("Using cached Chromium binary: %s", candidates[0])
            return pw.chromium.launch(headless=True, executable_path=str(candidates[0]))
        raise exc


# ---------------------------------------------------------------------------
# 1. Reddit Global Harvester
# ---------------------------------------------------------------------------

def scrape_reddit_global(page: Page) -> list[dict]:
    """
    Scrape targeted subreddits (/new/) and old.reddit.com/search (t=day, max 15/query)
    across all niche keywords, ADMIN_GLOBAL_QUERIES, and DISTRIBUTOR_QUERIES.
    """
    leads: list[dict] = []

    # Build subreddit map
    sub_to_niches: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    # Build global search query list: (query, niche, category)
    search_jobs: list[tuple[str, str, str]] = []
    seen_queries: set[tuple[str, str]] = set()

    for niche_key, cfg in NICHES.items():
        keywords = cfg.get("keywords", [])
        category = "agency_prospect" if niche_key == "admin_growth" else "client_lead"
        for sub in cfg.get("subreddits", []):
            sub_to_niches[sub].append((niche_key, keywords))
        for kw in keywords:
            if (kw.lower(), category) not in seen_queries:
                seen_queries.add((kw.lower(), category))
                search_jobs.append((kw, niche_key, category))

    for q in ADMIN_GLOBAL_QUERIES:
        if (q.lower(), "agency_prospect") not in seen_queries:
            seen_queries.add((q.lower(), "agency_prospect"))
            search_jobs.append((q, "admin_growth", "agency_prospect"))

    for q in DISTRIBUTOR_QUERIES:
        if (q.lower(), "distributor_prospect") not in seen_queries:
            seen_queries.add((q.lower(), "distributor_prospect"))
            search_jobs.append((q, "admin_growth", "distributor_prospect"))

    # Visit subreddit /new/ feeds
    for subreddit, niche_entries in sub_to_niches.items():
        try:
            leads.extend(_scrape_reddit_subreddit(page, subreddit, niche_entries))
        except Exception as exc:
            logger.warning("Failed to scrape r/%s: %s", subreddit, exc)

    # Visit global search (last 24h)
    for query, niche_key, category in search_jobs:
        try:
            leads.extend(_scrape_reddit_search_query(page, query, niche_key, category))
        except Exception as exc:
            logger.warning("Failed Reddit global search '%s': %s", query, exc)

    return leads


def _scrape_reddit_subreddit(
    page: Page,
    subreddit: str,
    niche_entries: list[tuple[str, list[str]]],
) -> list[dict]:
    """Navigate to old.reddit.com/r/{sub}/new/ (with www.reddit.com shreddit fallback) and harvest matching posts."""
    url = OLD_REDDIT_NEW_URL.format(sub=subreddit)
    logger.info("Visiting r/%s …", subreddit)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except PWTimeout:
        logger.warning("Timeout navigating to r/%s — skipping.", subreddit)
        return []

    page.wait_for_timeout(SETTLE_WAIT_MS)
    post_elements = page.query_selector_all("div.thing:not(.promoted)")

    candidates: list[dict] = []
    if post_elements and "/login" not in page.url:
        for el in post_elements[:MAX_POSTS_PER_SUB]:
            try:
                post_id = el.get_attribute("data-fullname") or ""
                if not post_id or is_post_processed(post_id):
                    continue

                title_el = el.query_selector("a.title")
                if not title_el:
                    continue

                title = (title_el.inner_text() or "").strip()
                raw_href = title_el.get_attribute("href") or ""

                matched_niches = [
                    niche_key
                    for niche_key, keywords in niche_entries
                    if _matches_keywords(title, keywords)
                ]

                if not matched_niches:
                    mark_post_processed(post_id, "")
                    continue

                permalink, old_permalink = _normalise_reddit_urls(raw_href)
                candidates.append(
                    {
                        "post_id": post_id,
                        "subreddit": subreddit,
                        "title": title,
                        "permalink": permalink,
                        "old_permalink": old_permalink,
                        "matched_niches": matched_niches,
                        "category": "agency_prospect" if matched_niches[0] == "admin_growth" else "client_lead",
                    }
                )
            except Exception as exc:
                logger.debug("Error inspecting post element in r/%s: %s", subreddit, exc)
        return _hydrate_reddit_candidates(page, candidates)

    # Fallback to www.reddit.com (shreddit-post) when old.reddit.com enforces login wall
    www_url = WWW_REDDIT_NEW_URL.format(sub=subreddit)
    try:
        page.goto(www_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(3000)
        page.evaluate("window.scrollBy(0, 1400)")
        page.wait_for_timeout(1500)
    except Exception as exc:
        logger.warning("Fallback www.reddit.com failed for r/%s: %s", subreddit, exc)
        return []

    shreddit_posts = page.query_selector_all("shreddit-post")
    for sp in shreddit_posts[:MAX_POSTS_PER_SUB]:
        try:
            post_id = sp.get_attribute("id") or ""
            if not post_id or is_post_processed(post_id):
                continue
            title = (sp.get_attribute("post-title") or "").strip()
            raw_href = sp.get_attribute("permalink") or ""
            if not title:
                continue

            matched_niches = [
                niche_key
                for niche_key, keywords in niche_entries
                if _matches_keywords(title, keywords)
            ]
            if not matched_niches:
                mark_post_processed(post_id, "")
                continue

            permalink, _ = _normalise_reddit_urls(raw_href)
            candidates.append(
                {
                    "post_id": post_id,
                    "subreddit": subreddit,
                    "title": title,
                    "permalink": permalink,
                    "old_permalink": permalink,
                    "matched_niches": matched_niches,
                    "category": "agency_prospect" if matched_niches[0] == "admin_growth" else "client_lead",
                }
            )
        except Exception as exc:
            logger.debug("Error inspecting shreddit-post in r/%s: %s", subreddit, exc)

    return _hydrate_reddit_candidates(page, candidates)


def _scrape_reddit_search_query(
    page: Page,
    query: str,
    niche_key: str,
    category: str,
) -> list[dict]:
    """Search old.reddit.com/search (last 24h) for *query*."""
    url = OLD_REDDIT_SEARCH_URL.format(query=quote_plus(query))
    logger.info("Searching Reddit (24h) [%s]: '%s' …", category, query)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except PWTimeout:
        logger.warning("Timeout on Reddit search '%s' — skipping.", query)
        return []

    page.wait_for_timeout(SETTLE_WAIT_MS)
    elements = page.query_selector_all("div.search-result")
    if not elements:
        elements = page.query_selector_all("div.thing:not(.promoted)")

    candidates: list[dict] = []
    for el in elements[:MAX_SEARCH_RESULTS]:
        try:
            title_el = el.query_selector("a.search-title") or el.query_selector("a.title")
            if not title_el:
                continue

            title = (title_el.inner_text() or "").strip()
            raw_href = title_el.get_attribute("href") or ""

            post_id = el.get_attribute("data-fullname") or _extract_post_id(raw_href)
            if not post_id or is_post_processed(post_id):
                continue

            sub_el = el.query_selector("a.search-subreddit-link") or el.query_selector("a.subreddit")
            subreddit = ""
            if sub_el:
                subreddit = (sub_el.inner_text() or "").strip().removeprefix("r/")
            if not subreddit:
                subreddit = _extract_subreddit(raw_href)

            permalink, old_permalink = _normalise_reddit_urls(raw_href)
            candidates.append(
                {
                    "post_id": post_id,
                    "subreddit": subreddit or "reddit",
                    "title": title,
                    "permalink": permalink,
                    "old_permalink": old_permalink,
                    "matched_niches": [niche_key],
                    "category": category,
                }
            )
        except Exception as exc:
            logger.debug("Error inspecting search result '%s': %s", query, exc)

    return _hydrate_reddit_candidates(page, candidates)


def _hydrate_reddit_candidates(page: Page, candidates: list[dict]) -> list[dict]:
    """Fetch full post body for matched Reddit candidates, log titles, and emit leads."""
    leads: list[dict] = []
    for item in candidates:
        selftext = _fetch_post_body(page, item["old_permalink"])
        primary_niche = item["matched_niches"][0]
        mark_post_processed(item["post_id"], primary_niche)

        for niche_key in item["matched_niches"]:
            cat = "agency_prospect" if niche_key == "admin_growth" and item["category"] == "client_lead" else item["category"]
            logger.info(
                "MATCHED LEAD [Reddit | %s | %s] r/%s: %s",
                cat,
                niche_key,
                item["subreddit"],
                item["title"],
            )
            leads.append(
                {
                    "post_id": item["post_id"],
                    "niche": niche_key,
                    "platform": "Reddit",
                    "category": cat,
                    "subreddit": item["subreddit"],
                    "title": item["title"],
                    "selftext": selftext[:2000],
                    "url": item["permalink"],
                }
            )
    return leads


# ---------------------------------------------------------------------------
# 2. Hacker News Live API Harvester
# ---------------------------------------------------------------------------

def scrape_hackernews_api() -> list[dict]:
    """
    Query hn.algolia.com/api/v1/search_by_date for the last 24 hours across
    agency/SaaS lead-gen and distributor queries.
    """
    unix_24h_ago = int(time.time()) - 86_400
    leads: list[dict] = []

    hn_queries: list[tuple[str, str]] = (
        [(q, "agency_prospect") for q in ADMIN_GLOBAL_QUERIES]
        + [(q, "distributor_prospect") for q in DISTRIBUTOR_QUERIES]
        + [
            ("looking for developer", "client_lead"),
            ("need a developer", "client_lead"),
            ("build an mvp", "client_lead"),
        ]
    )

    headers = {"User-Agent": USER_AGENT}
    for query, category in hn_queries:
        try:
            params = {
                "query": query,
                "tags": "(story,comment)",
                "numericFilters": f"created_at_i>{unix_24h_ago}",
                "hitsPerPage": 15,
            }
            resp = requests.get(HN_API_URL, params=params, headers=headers, timeout=10)
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
        except Exception as exc:
            logger.warning("HN API query failed for '%s': %s", query, exc)
            continue

        for hit in hits:
            object_id = str(hit.get("objectID") or "")
            if not object_id:
                continue

            post_id = f"hn_{object_id}"
            if is_post_processed(post_id):
                continue

            title = (hit.get("title") or hit.get("story_title") or "").strip()
            comment_text = _strip_html(hit.get("comment_text") or hit.get("story_text") or "")
            if not title and comment_text:
                title = comment_text[:120]
            if not title:
                continue

            hn_url = f"https://news.ycombinator.com/item?id={object_id}"
            niche = "web_dev" if category == "client_lead" else "admin_growth"
            mark_post_processed(post_id, niche)

            logger.info("MATCHED LEAD [HackerNews | %s]: %.80s", category, title)
            leads.append(
                {
                    "post_id": post_id,
                    "niche": niche,
                    "platform": "HackerNews",
                    "category": category,
                    "subreddit": "HackerNews",
                    "title": title,
                    "selftext": comment_text[:2000],
                    "url": hn_url,
                }
            )

    return leads


# ---------------------------------------------------------------------------
# 3. Web / Social Harvester (X / Twitter & IndieHackers via DuckDuckGo HTML)
# ---------------------------------------------------------------------------

def scrape_web_social_global(page: Page) -> list[dict]:
    """
    Use Playwright on DuckDuckGo HTML (`html.duckduckgo.com/html/`) to extract
    direct links and snippets from X / Twitter and IndieHackers.
    """
    leads: list[dict] = []

    for entry in WEB_SOCIAL_QUERIES:
        platform = entry["platform"]
        query = entry["query"]
        url = f"{DDG_HTML_URL}?q={quote_plus(query)}"
        logger.info("Searching DuckDuckGo [%s]: %s …", platform, query)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(SETTLE_WAIT_MS)
        except PWTimeout:
            logger.warning("Timeout on DuckDuckGo search for %s — skipping.", platform)
            continue

        results = page.query_selector_all("div.result")
        for res in results[:15]:
            try:
                link_el = res.query_selector("a.result__a")
                if not link_el:
                    continue

                title = (link_el.inner_text() or "").strip()
                raw_href = link_el.get_attribute("href") or ""
                direct_url = _extract_ddg_url(raw_href)
                if not direct_url.startswith("http"):
                    continue

                post_id = "ddg_" + hashlib.sha1(direct_url.encode("utf-8")).hexdigest()[:12]
                if is_post_processed(post_id):
                    continue

                snippet_el = res.query_selector("a.result__snippet") or res.query_selector(".result__snippet")
                snippet = (snippet_el.inner_text() or "").strip() if snippet_el else ""

                mark_post_processed(post_id, "admin_growth")
                logger.info("MATCHED LEAD [%s | agency_prospect]: %.80s", platform, title)

                leads.append(
                    {
                        "post_id": post_id,
                        "niche": "admin_growth",
                        "platform": platform,
                        "category": "agency_prospect",
                        "subreddit": platform,
                        "title": title,
                        "selftext": snippet[:2000],
                        "url": direct_url,
                    }
                )
            except Exception as exc:
                logger.debug("Error parsing DuckDuckGo result for %s: %s", platform, exc)

    return leads


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_ddg_url(href: str) -> str:
    """Unwrap DuckDuckGo `/l/?uddg=...` redirect URLs into direct target URLs."""
    if "uddg=" in href:
        parsed = urlparse(href)
        uddg = parse_qs(parsed.query).get("uddg", [])
        if uddg:
            return unquote(uddg[0])
    if href.startswith("//"):
        return "https:" + href
    return href


def _strip_html(raw: str) -> str:
    """Remove basic HTML tags from Hacker News Algolia snippets."""
    return re.sub(r"<[^>]+>", " ", raw).strip()


def _extract_post_id(url: str) -> str:
    match = _COMMENT_ID_RE.search(url)
    return f"t3_{match.group(1)}" if match else ""


def _extract_subreddit(url: str) -> str:
    match = _SUBREDDIT_RE.search(url)
    return match.group(1) if match else ""


def _normalise_reddit_urls(raw_href: str) -> tuple[str, str]:
    if raw_href.startswith("/r/"):
        return REDDIT_BASE + raw_href, OLD_REDDIT_BASE + raw_href
    if raw_href.startswith("https://www.reddit.com"):
        return raw_href, raw_href.replace("https://www.reddit.com", OLD_REDDIT_BASE, 1)
    if raw_href.startswith("https://old.reddit.com"):
        return raw_href.replace(OLD_REDDIT_BASE, REDDIT_BASE, 1), raw_href
    return raw_href, raw_href


def _fetch_post_body(page: Page, old_reddit_url: str) -> str:
    if not old_reddit_url.startswith("http"):
        return ""
    try:
        page.goto(old_reddit_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(SETTLE_WAIT_MS)
        body_el = page.query_selector("div.expando div.usertext-body")
        if body_el:
            return (body_el.inner_text() or "").strip()
    except Exception as exc:
        logger.debug("Could not fetch post body from %s: %s", old_reddit_url, exc)
    return ""


def _matches_keywords(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(kw.lower() in lowered for kw in keywords)


# ---------------------------------------------------------------------------
# Live Diagnostic Test Scrape (/test_scrape)
# ---------------------------------------------------------------------------

def run_test_scrape(niche: str = "seo_mkt") -> dict:
    """
    Run an immediate live Playwright scrape on old.reddit.com for *niche*
    (without skipping/marking processed_posts) and return status + top 2 matches.
    """
    cfg = NICHES.get(niche) or NICHES.get("seo_mkt", {})
    keywords = cfg.get("keywords", ["need seo", "looking for marketing agency"])
    subreddits = list(dict.fromkeys((cfg.get("subreddits") or [])[:3] + ["SaaS", "Entrepreneur", "marketing"]))[:3]

    details: list[dict] = []
    matches: list[dict] = []
    total_scanned = 0

    try:
        with sync_playwright() as pw:
            browser: Browser = _launch_browser(pw)
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                )
                page: Page = context.new_page()

                for sub in subreddits:
                    url = OLD_REDDIT_NEW_URL.format(sub=sub)
                    status_code: int | None = None
                    sub_count = 0
                    sub_err: str | None = None

                    try:
                        resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                        status_code = resp.status if resp else None
                        page.wait_for_timeout(SETTLE_WAIT_MS)

                        post_elements = page.query_selector_all("div.thing:not(.promoted)")
                        if post_elements and "/login" not in page.url:
                            sub_count = len(post_elements)
                            total_scanned += sub_count
                            for el in post_elements[:MAX_POSTS_PER_SUB]:
                                title_el = el.query_selector("a.title")
                                if not title_el:
                                    continue
                                title = (title_el.inner_text() or "").strip()
                                raw_href = title_el.get_attribute("href") or ""
                                permalink, _ = _normalise_reddit_urls(raw_href)
                                matched_kw = next(
                                    (kw for kw in keywords if kw.lower() in title.lower()),
                                    None,
                                )
                                if matched_kw:
                                    matches.append(
                                        {
                                            "subreddit": sub,
                                            "title": title,
                                            "url": permalink,
                                            "matched_keyword": matched_kw,
                                        }
                                    )
                        else:
                            # old.reddit.com redirected to login wall -> fall back to www.reddit.com
                            www_url = WWW_REDDIT_NEW_URL.format(sub=sub)
                            resp = page.goto(www_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                            status_code = resp.status if resp else status_code
                            page.wait_for_timeout(3000)
                            page.evaluate("window.scrollBy(0, 1400)")
                            page.wait_for_timeout(1500)
                            shreddit_posts = page.query_selector_all("shreddit-post")
                            sub_count = len(shreddit_posts)
                            total_scanned += sub_count
                            for sp in shreddit_posts[:MAX_POSTS_PER_SUB]:
                                title = (sp.get_attribute("post-title") or "").strip()
                                raw_href = sp.get_attribute("permalink") or ""
                                if not title:
                                    continue
                                permalink, _ = _normalise_reddit_urls(raw_href)
                                matched_kw = next(
                                    (kw for kw in keywords if kw.lower() in title.lower()),
                                    None,
                                )
                                if matched_kw:
                                    matches.append(
                                        {
                                            "subreddit": sub,
                                            "title": title,
                                            "url": permalink,
                                            "matched_keyword": matched_kw,
                                        }
                                    )

                        print(
                            f"[TEST_SCRAPE] r/{sub} -> HTTP {status_code} | {sub_count} posts scanned",
                            flush=True,
                        )
                        logger.info(
                            "[TEST_SCRAPE] r/%s -> HTTP %s | %d posts scanned",
                            sub,
                            status_code,
                            sub_count,
                        )
                    except Exception as exc:
                        sub_err = str(exc)
                        logger.warning("[TEST_SCRAPE] r/%s error: %s", sub, exc)

                    details.append(
                        {
                            "subreddit": sub,
                            "status": status_code,
                            "posts_scanned": sub_count,
                            "error": sub_err,
                        }
                    )
            finally:
                browser.close()

        return {
            "niche": niche,
            "keywords": keywords,
            "subreddits": subreddits,
            "details": details,
            "total_scanned": total_scanned,
            "matches": matches[:2],
            "total_matches": len(matches),
            "error": None,
        }
    except Exception as exc:
        logger.error("[TEST_SCRAPE] Fatal error: %s", exc)
        return {
            "niche": niche,
            "keywords": keywords,
            "subreddits": subreddits,
            "details": details,
            "total_scanned": total_scanned,
            "matches": [],
            "total_matches": 0,
            "error": str(exc),
        }

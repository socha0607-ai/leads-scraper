"""
scraper.py — Multi-Platform Global Harvester (Reddit, X, Hacker News, Indie Hackers, Product Hunt).

Combines five scraping engines into `scrape_all_platforms()`:
  1. `scrape_hackernews_api()`        — Fast JSON API via hn.algolia.com (last 24h, no browser)
  2. `scrape_reddit_global(page)`     — Playwright on old.reddit.com/r/{sub}/new/ & old.reddit.com/search
  3. `scrape_indiehackers(page)`      — Playwright on indiehackers.com/newest & /groups
  4. `scrape_producthunt(page)`       — Playwright on producthunt.com/discussions
  5. `scrape_x_global(context, page)` — Authenticated Playwright on x.com/search (live) & /i/communities

Global Playwright Speed Optimization:
  - Blocks `image`, `stylesheet`, `font`, and `media` requests via `page.route("**/*", ...)`.
  - Uses `wait_until="domcontentloaded"` with `timeout=15000` everywhere.

Every item is deduplicated against `leads.db` and tagged with:
  - `platform`: "Reddit" | "X" | "Hacker News" | "Indie Hackers" | "Product Hunt"
  - `group_or_sub`: e.g. "r/SEO", "X Community", "Ask HN", "Indie Hackers", "Product Hunt"
  - `category`: "client_lead" | "agency_prospect" | "distributor_prospect"
"""

import hashlib
import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote_plus

import requests
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Route,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from config import (
    ADMIN_GLOBAL_QUERIES,
    DISTRIBUTOR_QUERIES,
    HN_API_URL,
    INDIEHACKERS_GROUPS_URL,
    INDIEHACKERS_NEWEST_URL,
    NICHES,
    PLAYWRIGHT_TIMEOUT_MS,
    PRODUCTHUNT_DISCUSSIONS_URL,
    USER_AGENT,
    X_AUTH_TOKEN,
    X_CT0,
    X_SEARCH_QUERIES,
)
from database import is_post_processed, mark_post_processed

logger = logging.getLogger(__name__)

OLD_REDDIT_NEW_URL = "https://old.reddit.com/r/{sub}/new/"
OLD_REDDIT_JSON_URL = "https://old.reddit.com/r/{sub}/new.json?limit=25"
OLD_REDDIT_SEARCH_URL = "https://old.reddit.com/search?q={query}&sort=new&t=day"
OLD_REDDIT_BASE = "https://old.reddit.com"

STEALTH_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

X_SEARCH_URL = "https://x.com/search?q={query}&f=live"
X_COMMUNITIES_URL = "https://x.com/i/communities"
PH_GRAPHQL_URL = "https://www.producthunt.com/frontend/graphql"

MAX_POSTS_PER_SUB = 25
MAX_SEARCH_RESULTS = 15
NAV_TIMEOUT_MS = PLAYWRIGHT_TIMEOUT_MS  # 15000 ms
SETTLE_WAIT_MS = 1_500
BLOCKED_RESOURCE_TYPES = {"image", "stylesheet", "font", "media"}

_COMMENT_ID_RE = re.compile(r"/comments/([a-z0-9]+)/", re.IGNORECASE)
_SUBREDDIT_RE = re.compile(r"/r/([^/]+)/", re.IGNORECASE)


def _block_heavy_assets(route: Route) -> None:
    """Abort image, stylesheet, font, and media requests to prevent Playwright hangs."""
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def _new_fast_page(context: BrowserContext) -> Page:
    """Create a Playwright page with global asset blocking and 15s default timeout."""
    page = context.new_page()
    page.set_default_timeout(NAV_TIMEOUT_MS)
    page.route("**/*", _block_heavy_assets)
    return page


def _launch_browser(pw) -> Browser:
    """Launch Chromium with stealth args, falling back to cached headless shell if needed."""
    launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    try:
        return pw.chromium.launch(headless=True, args=launch_args)
    except Exception as exc:
        cache_dir = Path.home() / ".cache" / "ms-playwright"
        candidates = sorted(
            cache_dir.glob("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"),
            reverse=True,
        )
        if candidates:
            logger.info("Using cached Chromium binary: %s", candidates[0])
            return pw.chromium.launch(
                headless=True,
                executable_path=str(candidates[0]),
                args=launch_args,
            )
        raise exc


def _new_stealth_context(browser: Browser) -> BrowserContext:
    """Create a stealth desktop BrowserContext with Accept-Language & webdriver masking."""
    context = browser.new_context(
        user_agent=STEALTH_USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return context


def scrape_all_platforms() -> list[dict]:
    """
    Run all five platform harvesters (Hacker News, Reddit, Indie Hackers,
    Product Hunt, and authenticated X / Communities) and return deduplicated leads.
    """
    leads: list[dict] = []

    # 1. Fast JSON API: Hacker News (no browser needed)
    try:
        leads.extend(scrape_hackernews_api())
    except Exception as exc:
        logger.warning("Hacker News scraper failed: %s", exc)

    # 2. Browser-based harvesters with stealth context, asset blocking & 15s timeout
    with sync_playwright() as pw:
        browser: Browser = _launch_browser(pw)
        try:
            context = _new_stealth_context(browser)
            page: Page = _new_fast_page(context)

            for name, fn in (
                ("Reddit", lambda: scrape_reddit_global(page)),
                ("Indie Hackers", lambda: scrape_indiehackers(page)),
                ("Product Hunt", lambda: scrape_producthunt(page)),
                ("X / Communities", lambda: scrape_x_global(context, page)),
            ):
                try:
                    leads.extend(fn())
                except Exception as exc:
                    logger.warning("%s scraper failed: %s", name, exc)
        finally:
            browser.close()

    logger.info("Multi-platform harvest complete. %d new lead(s) queued for AI.", len(leads))
    return leads


# Backward-compatible alias
scrape_subreddits = scrape_all_platforms


# ---------------------------------------------------------------------------
# 1. Reddit Global Harvester (old.reddit.com ONLY)
# ---------------------------------------------------------------------------

def scrape_reddit_global(page: Page) -> list[dict]:
    """
    Scrape targeted subreddits on old.reddit.com/r/{sub}/new/ and
    old.reddit.com/search (t=day) across all niche keywords.
    """
    leads: list[dict] = []

    sub_to_niches: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
    search_jobs: list[tuple[str, str, str]] = []
    seen_queries: set[tuple[str, str]] = set()

    for niche_key, cfg in NICHES.items():
        keywords = cfg.get("keywords", [])
        category = "agency_prospect" if niche_key == "admin_growth" else "client_lead"
        for sub in cfg.get("subreddits", []):
            sub_to_niches[sub].append((niche_key, keywords))
        for kw in keywords[:6]:
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

    for subreddit, niche_entries in sub_to_niches.items():
        try:
            leads.extend(_scrape_reddit_subreddit(page, subreddit, niche_entries))
        except Exception as exc:
            logger.warning("Failed to scrape r/%s: %s", subreddit, exc)

    for query, niche_key, category in search_jobs:
        try:
            leads.extend(_scrape_reddit_search_query(page, query, niche_key, category))
        except Exception as exc:
            logger.warning("Failed Reddit global search '%s': %s", query, exc)

    return leads


def _fetch_reddit_subreddit_posts(
    page: Page,
    subreddit: str,
) -> tuple[list[dict], int | None, str | None]:
    """
    Fetch posts from old.reddit.com/r/{sub}/new.json?limit=25 with a realistic
    User-Agent, falling back to Playwright HTML parsing on old.reddit.com/r/{sub}/new/.
    Returns (posts, status_code, error). Each post has post_id, title, selftext, url.
    """
    json_url = OLD_REDDIT_JSON_URL.format(sub=subreddit)
    headers = {
        "User-Agent": STEALTH_USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }
    status_code: int | None = None

    try:
        resp = requests.get(json_url, headers=headers, timeout=10)
        status_code = resp.status_code
        if resp.status_code == 200:
            children = resp.json().get("data", {}).get("children", [])
            posts: list[dict] = []
            for child in children[:MAX_POSTS_PER_SUB]:
                data = child.get("data") or {}
                post_id = data.get("name") or (f"t3_{data['id']}" if data.get("id") else "")
                title = (data.get("title") or "").strip()
                selftext = (data.get("selftext") or "").strip()
                permalink = data.get("permalink") or ""
                full_url = (
                    f"{OLD_REDDIT_BASE}{permalink}"
                    if permalink.startswith("/")
                    else (data.get("url") or "")
                )
                if post_id and title:
                    posts.append(
                        {
                            "post_id": post_id,
                            "title": title,
                            "selftext": selftext,
                            "url": full_url,
                        }
                    )
            if posts:
                return posts, status_code, None
    except Exception as exc:
        logger.debug("Reddit JSON fetch failed for r/%s (%s) — trying HTML.", subreddit, exc)

    # Fallback: Playwright on old.reddit.com/r/{sub}/new/
    html_url = OLD_REDDIT_NEW_URL.format(sub=subreddit)
    try:
        pw_resp = page.goto(html_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        status_code = pw_resp.status if pw_resp else status_code
        page.wait_for_timeout(SETTLE_WAIT_MS)
        if "/login" in page.url:
            return [], status_code, "old.reddit.com login wall"

        posts = []
        for el in page.query_selector_all("div.thing:not(.promoted)")[:MAX_POSTS_PER_SUB]:
            post_id = el.get_attribute("data-fullname") or ""
            title_el = el.query_selector("a.title")
            if not post_id or not title_el:
                continue
            title = (title_el.inner_text() or "").strip()
            raw_href = title_el.get_attribute("href") or ""
            expando = el.query_selector("div.expando div.usertext-body")
            selftext = (expando.inner_text() or "").strip() if expando else ""
            posts.append(
                {
                    "post_id": post_id,
                    "title": title,
                    "selftext": selftext,
                    "url": _normalise_old_reddit_url(raw_href),
                }
            )
        return posts, status_code, None
    except PWTimeout:
        return [], status_code, f"Timeout navigating to r/{subreddit}"
    except Exception as exc:
        return [], status_code, str(exc)


def _scrape_reddit_subreddit(
    page: Page,
    subreddit: str,
    niche_entries: list[tuple[str, list[str]]],
) -> list[dict]:
    """Fetch r/{sub} via old.reddit.com JSON/HTML and harvest keyword-matched posts."""
    logger.info("Visiting old.reddit.com/r/%s/new.json …", subreddit)
    raw_posts, _, err = _fetch_reddit_subreddit_posts(page, subreddit)
    if err:
        logger.warning("Reddit r/%s skipped: %s", subreddit, err)
        return []

    leads: list[dict] = []
    for item in raw_posts:
        post_id = item["post_id"]
        if is_post_processed(post_id):
            continue

        combined = f"{item['title']} {item['selftext']}"
        matched_niches = [
            niche_key
            for niche_key, keywords in niche_entries
            if _matches_keywords(combined, keywords)
        ]
        if not matched_niches:
            mark_post_processed(post_id, "")
            continue

        selftext = item["selftext"] or _fetch_post_body(page, item["url"])
        primary_niche = matched_niches[0]
        mark_post_processed(post_id, primary_niche)

        for niche_key in matched_niches:
            cat = "agency_prospect" if niche_key == "admin_growth" else "client_lead"
            logger.info(
                "MATCHED LEAD [Reddit | %s | %s] r/%s: %s",
                cat,
                niche_key,
                subreddit,
                item["title"],
            )
            leads.append(
                {
                    "post_id": post_id,
                    "niche": niche_key,
                    "platform": "Reddit",
                    "group_or_sub": f"r/{subreddit}",
                    "category": cat,
                    "subreddit": subreddit,
                    "title": item["title"],
                    "selftext": selftext[:2000],
                    "url": item["url"],
                }
            )

    return leads


def _scrape_reddit_search_query(
    page: Page,
    query: str,
    niche_key: str,
    category: str,
) -> list[dict]:
    """Search old.reddit.com/search (last 24h) for *query*."""
    url = OLD_REDDIT_SEARCH_URL.format(query=quote_plus(query))
    logger.info("Searching old.reddit.com (24h) [%s]: '%s' …", category, query)

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
                subreddit = _extract_subreddit(raw_href) or "reddit"

            old_permalink = _normalise_old_reddit_url(raw_href)
            candidates.append(
                {
                    "post_id": post_id,
                    "subreddit": subreddit,
                    "group_or_sub": f"r/{subreddit}",
                    "title": title,
                    "permalink": old_permalink,
                    "old_permalink": old_permalink,
                    "matched_niches": [niche_key],
                    "category": category,
                }
            )
        except Exception as exc:
            logger.debug("Error inspecting search result '%s': %s", query, exc)

    return _hydrate_reddit_candidates(page, candidates)


def _hydrate_reddit_candidates(page: Page, candidates: list[dict]) -> list[dict]:
    """Fetch full post body for matched Reddit candidates and emit tagged leads."""
    leads: list[dict] = []
    for item in candidates:
        selftext = _fetch_post_body(page, item["old_permalink"])
        primary_niche = item["matched_niches"][0]
        mark_post_processed(item["post_id"], primary_niche)

        for niche_key in item["matched_niches"]:
            cat = (
                "agency_prospect"
                if niche_key == "admin_growth" and item["category"] == "client_lead"
                else item["category"]
            )
            logger.info(
                "MATCHED LEAD [Reddit | %s | %s] %s: %s",
                cat,
                niche_key,
                item["group_or_sub"],
                item["title"],
            )
            leads.append(
                {
                    "post_id": item["post_id"],
                    "niche": niche_key,
                    "platform": "Reddit",
                    "group_or_sub": item["group_or_sub"],
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
    Fetch real-time stories & comments via hn.algolia.com/api/v1/search_by_date
    for the last 24h using `requests` (no browser needed) and match niche keywords.
    """
    unix_24h_ago = int(time.time()) - 86_400
    leads: list[dict] = []
    headers = {"User-Agent": USER_AGENT}

    params = {
        "tags": "(story,comment,ask_hn)",
        "numericFilters": f"created_at_i>{unix_24h_ago}",
        "hitsPerPage": 100,
    }

    try:
        resp = requests.get(HN_API_URL, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
    except Exception as exc:
        logger.warning("HN Algolia feed fetch failed: %s", exc)
        hits = []

    for hit in hits:
        object_id = str(hit.get("objectID") or "")
        if not object_id:
            continue

        post_id = f"hn_{object_id}"
        if is_post_processed(post_id):
            continue

        title = (hit.get("title") or hit.get("story_title") or "").strip()
        comment_text = _strip_html(hit.get("comment_text") or hit.get("story_text") or "")
        combined_text = f"{title} {comment_text}".strip()
        if not combined_text:
            continue
        if not title:
            title = comment_text[:120]

        niche, category = _classify_text_to_niche(combined_text)
        if not niche:
            continue

        tags = hit.get("_tags") or []
        if "ask_hn" in tags or title.lower().startswith("ask hn"):
            group_or_sub = "Ask HN"
        elif "comment" in tags:
            group_or_sub = "HN Comment"
        else:
            group_or_sub = "Hacker News"

        hn_url = f"https://news.ycombinator.com/item?id={object_id}"
        mark_post_processed(post_id, niche)

        logger.info("MATCHED LEAD [Hacker News | %s | %s]: %.80s", group_or_sub, niche, title)
        leads.append(
            {
                "post_id": post_id,
                "niche": niche,
                "platform": "Hacker News",
                "group_or_sub": group_or_sub,
                "category": category,
                "subreddit": group_or_sub,
                "title": title,
                "selftext": comment_text[:2000],
                "url": hn_url,
            }
        )

    return leads


# ---------------------------------------------------------------------------
# 3. Indie Hackers Harvester (Playwright + Asset Blocking)
# ---------------------------------------------------------------------------

def scrape_indiehackers(page: Page) -> list[dict]:
    """Scrape indiehackers.com/newest and /groups using Playwright with asset blocking."""
    leads: list[dict] = []
    targets = [
        (INDIEHACKERS_NEWEST_URL, "Indie Hackers"),
        (INDIEHACKERS_GROUPS_URL, "IH Groups"),
    ]

    for url, group_label in targets:
        items, status, err = _fetch_indiehackers_items(page, url)
        if err:
            logger.warning("Indie Hackers (%s) error: %s", url, err)
            continue
        logger.info("Indie Hackers (%s) -> HTTP %s | %d items", group_label, status, len(items))

        for item in items:
            post_id = "ih_" + hashlib.sha1(item["url"].encode("utf-8")).hexdigest()[:12]
            if is_post_processed(post_id):
                continue

            combined = f"{item['title']} {item['body']}"
            niche, category = _classify_text_to_niche(combined)
            if not niche:
                continue

            mark_post_processed(post_id, niche)
            logger.info("MATCHED LEAD [Indie Hackers | %s]: %.80s", niche, item["title"])
            leads.append(
                {
                    "post_id": post_id,
                    "niche": niche,
                    "platform": "Indie Hackers",
                    "group_or_sub": group_label,
                    "category": category,
                    "subreddit": group_label,
                    "title": item["title"],
                    "selftext": item["body"][:2000],
                    "url": item["url"],
                }
            )

    return leads


def _fetch_indiehackers_items(page: Page, url: str) -> tuple[list[dict], int | None, str | None]:
    """Navigate to an Indie Hackers feed page and extract post titles, snippets, and URLs."""
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        status = resp.status if resp else None
        page.wait_for_timeout(SETTLE_WAIT_MS)
    except PWTimeout:
        return [], None, f"Timeout loading {url}"
    except Exception as exc:
        return [], None, str(exc)

    seen_urls: set[str] = set()
    items: list[dict] = []

    for link in page.query_selector_all('a[href*="/post/"]')[:40]:
        try:
            href = link.get_attribute("href") or ""
            if not href or "/post/" not in href:
                continue
            full_url = href if href.startswith("http") else f"https://www.indiehackers.com{href}"
            full_url = full_url.split("#")[0].split("?")[0]
            if full_url in seen_urls:
                continue

            title = (link.inner_text() or "").strip()
            if len(title) < 8:
                continue
            seen_urls.add(full_url)

            body = ""
            parent_text = link.evaluate(
                "el => el.closest('article, .feed-item, .post-item, div')?.innerText || ''"
            )
            if parent_text:
                body = parent_text.strip()[:1000]

            items.append({"title": title, "body": body, "url": full_url})
        except Exception:
            continue

    return items, status, None


# ---------------------------------------------------------------------------
# 4. Product Hunt Harvester (Playwright + Asset Blocking)
# ---------------------------------------------------------------------------

def scrape_producthunt(page: Page) -> list[dict]:
    """Scrape producthunt.com/discussions using Playwright with asset blocking."""
    leads: list[dict] = []
    items, status, err = _fetch_producthunt_items(page, PRODUCTHUNT_DISCUSSIONS_URL)
    if err:
        logger.warning("Product Hunt error: %s", err)
        return []

    logger.info("Product Hunt Discussions -> HTTP %s | %d items", status, len(items))
    for item in items:
        post_id = "ph_" + hashlib.sha1(item["url"].encode("utf-8")).hexdigest()[:12]
        if is_post_processed(post_id):
            continue

        combined = f"{item['title']} {item['body']}"
        niche, category = _classify_text_to_niche(combined)
        if not niche:
            continue

        mark_post_processed(post_id, niche)
        logger.info("MATCHED LEAD [Product Hunt | %s]: %.80s", niche, item["title"])
        leads.append(
            {
                "post_id": post_id,
                "niche": niche,
                "platform": "Product Hunt",
                "group_or_sub": "Product Hunt",
                "category": category,
                "subreddit": "Product Hunt",
                "title": item["title"],
                "selftext": item["body"][:2000],
                "url": item["url"],
            }
        )

    return leads


def _fetch_producthunt_items(page: Page, url: str) -> tuple[list[dict], int | None, str | None]:
    """
    Navigate to Product Hunt discussions with the stealth context (waiting for
    discussion selectors) and fall back to Product Hunt's GraphQL endpoint on 403/empty.
    """
    status: int | None = None
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        status = resp.status if resp else None
        try:
            page.wait_for_selector(
                '[data-test="discussion-item"], a[href*="/discussions/"]',
                timeout=4_000,
            )
        except PWTimeout:
            pass
        page.wait_for_timeout(1_500)
    except PWTimeout:
        return [], None, f"Timeout loading {url}"
    except Exception as exc:
        return [], None, str(exc)

    seen_urls: set[str] = set()
    items: list[dict] = []

    selectors = page.query_selector_all('[data-test="discussion-item"]')
    if not selectors:
        selectors = page.query_selector_all('a[href*="/discussions/"]')

    for node in selectors[:40]:
        try:
            link = node if (node.evaluate("el => el.tagName") == "A") else node.query_selector('a[href*="/discussions/"]')
            if not link:
                continue
            href = link.get_attribute("href") or ""
            if not href or href.rstrip("/") == "/discussions":
                continue
            full_url = href if href.startswith("http") else f"https://www.producthunt.com{href}"
            full_url = full_url.split("#")[0].split("?")[0]
            if full_url in seen_urls:
                continue

            title = (link.inner_text() or "").strip()
            if len(title) < 8:
                continue
            seen_urls.add(full_url)

            body = (
                node.evaluate("el => el.closest('article, section, div')?.innerText || el.innerText || ''")
                or ""
            ).strip()[:1000]
            items.append({"title": title, "body": body, "url": full_url})
        except Exception:
            continue

    if items:
        return items, status, None

    # Fallback: Product Hunt GraphQL endpoint with stealth headers
    gql_items, gql_status = _fetch_producthunt_graphql()
    if gql_items:
        return gql_items, gql_status, None
    return [], status or gql_status, f"HTTP {status} (0 discussion items parsed)" if status and status >= 400 else None


def _fetch_producthunt_graphql() -> tuple[list[dict], int | None]:
    """Query Product Hunt's frontend GraphQL endpoint for recent discussions."""
    query = """
    query DiscussionsFeed {
      discussions(first: 20) {
        edges {
          node {
            id
            title
            body
            url
          }
        }
      }
    }
    """
    headers = {
        "User-Agent": STEALTH_USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.producthunt.com",
        "Referer": "https://www.producthunt.com/discussions",
    }
    try:
        resp = requests.post(PH_GRAPHQL_URL, json={"query": query}, headers=headers, timeout=10)
        if resp.status_code != 200:
            return [], resp.status_code
        edges = resp.json().get("data", {}).get("discussions", {}).get("edges", [])
        items: list[dict] = []
        for edge in edges:
            node = (edge or {}).get("node") or {}
            title = (node.get("title") or "").strip()
            body = (node.get("body") or "").strip()
            url = (node.get("url") or "").strip()
            if title and url:
                items.append({"title": title, "body": body[:1000], "url": url})
        return items, resp.status_code
    except Exception:
        return [], None


# ---------------------------------------------------------------------------
# 5. X (Twitter) & Joined X Communities Harvester
# ---------------------------------------------------------------------------

def _get_x_credentials() -> tuple[str, str]:
    """Read optional X_AUTH_TOKEN and X_CT0 from env (or config fallback)."""
    auth_token = (os.getenv("X_AUTH_TOKEN") or X_AUTH_TOKEN or "").strip()
    ct0 = (os.getenv("X_CT0") or X_CT0 or "").strip()
    return auth_token, ct0


def _inject_x_cookies(context: BrowserContext, auth_token: str, ct0: str) -> None:
    """Inject authenticated cookies onto BOTH .x.com and .twitter.com domains."""
    cookies = []
    for domain in (".x.com", ".twitter.com"):
        cookies.extend(
            [
                {
                    "name": "auth_token",
                    "value": auth_token,
                    "domain": domain,
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                },
                {
                    "name": "ct0",
                    "value": ct0,
                    "domain": domain,
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                },
            ]
        )
    context.add_cookies(cookies)


def scrape_x_global(context: BrowserContext, page: Page) -> list[dict]:
    """
    Inject X_AUTH_TOKEN / X_CT0 cookies if present, scan live X search queries
    and the user's joined X Communities feed (`x.com/i/communities`).
    Gracefully skips if unset or cookies expired.
    """
    auth_token, ct0 = _get_x_credentials()
    if not auth_token or not ct0:
        logger.info("X_AUTH_TOKEN / X_CT0 not set — skipping X & Communities scraper.")
        return []

    _inject_x_cookies(context, auth_token, ct0)
    leads: list[dict] = []

    # 5a. Joined X Communities timeline
    comm_items, status, err = _fetch_x_tweets(page, X_COMMUNITIES_URL)
    if err:
        logger.warning("X Communities skipped: %s", err)
        if "login" in err.lower() or "expired" in err.lower():
            return []
    else:
        logger.info("X Communities -> HTTP %s | %d tweets", status, len(comm_items))
        for tw in comm_items:
            post_id = "x_" + hashlib.sha1(tw["url"].encode("utf-8")).hexdigest()[:12]
            if is_post_processed(post_id):
                continue

            niche, category = _classify_text_to_niche(tw["text"])
            if not niche:
                continue

            mark_post_processed(post_id, niche)
            logger.info("MATCHED LEAD [X | X Community | %s]: %.80s", niche, tw["text"])
            leads.append(
                {
                    "post_id": post_id,
                    "niche": niche,
                    "platform": "X",
                    "group_or_sub": "X Community",
                    "category": category,
                    "subreddit": "X Community",
                    "title": tw["text"][:140],
                    "selftext": tw["text"][:2000],
                    "url": tw["url"],
                }
            )

    # 5b. High-intent live X search queries
    for entry in X_SEARCH_QUERIES:
        search_url = X_SEARCH_URL.format(query=quote_plus(entry["query"]))
        tweets, status, err = _fetch_x_tweets(page, search_url)
        if err:
            logger.warning("X Search (%s) skipped: %s", entry["niche"], err)
            if "login" in err.lower() or "expired" in err.lower():
                break
            continue

        logger.info("X Search [%s] -> HTTP %s | %d tweets", entry["niche"], status, len(tweets))
        for tw in tweets:
            post_id = "x_" + hashlib.sha1(tw["url"].encode("utf-8")).hexdigest()[:12]
            if is_post_processed(post_id):
                continue

            niche, category = _classify_text_to_niche(tw["text"])
            resolved_niche = niche or entry["niche"]
            resolved_cat = category if niche else "client_lead"

            mark_post_processed(post_id, resolved_niche)
            logger.info("MATCHED LEAD [X | X Search | %s]: %.80s", resolved_niche, tw["text"])
            leads.append(
                {
                    "post_id": post_id,
                    "niche": resolved_niche,
                    "platform": "X",
                    "group_or_sub": "X Search",
                    "category": resolved_cat,
                    "subreddit": "X Search",
                    "title": tw["text"][:140],
                    "selftext": tw["text"][:2000],
                    "url": tw["url"],
                }
            )

    return leads


def _fetch_x_tweets(page: Page, url: str) -> tuple[list[dict], int | None, str | None]:
    """Navigate to an X timeline/search page, wait 3s for tweets to render, and extract text + URL."""
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        status = resp.status if resp else None
        try:
            page.wait_for_selector('article[data-testid="tweet"]', timeout=3_000)
        except PWTimeout:
            pass
        page.wait_for_timeout(3_000)
    except PWTimeout:
        return [], None, f"Timeout loading {url}"
    except Exception as exc:
        return [], None, str(exc)

    if "/login" in page.url or "/i/flow/login" in page.url:
        return [], status, "X cookies expired or redirected to login"

    articles = page.query_selector_all('article[data-testid="tweet"]')
    if not articles:
        login_input = page.query_selector('input[name="text"]')
        if login_input:
            return [], status, "X cookies expired (login prompt shown)"
        return [], status, None

    tweets: list[dict] = []
    seen_urls: set[str] = set()
    for art in articles[:25]:
        try:
            text_el = art.query_selector('div[data-testid="tweetText"]')
            text = (text_el.inner_text() or "").strip() if text_el else ""
            if len(text) < 10:
                continue

            link_el = art.query_selector('a[href*="/status/"]')
            href = link_el.get_attribute("href") if link_el else ""
            if not href:
                continue
            tweet_url = href if href.startswith("http") else f"https://x.com{href}"
            tweet_url = tweet_url.split("?")[0]
            if tweet_url in seen_urls:
                continue
            seen_urls.add(tweet_url)

            tweets.append({"text": text, "url": tweet_url})
        except Exception:
            continue

    return tweets, status, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _classify_text_to_niche(text: str) -> tuple[str | None, str]:
    """Return (niche_key, category) for the first matching niche, or (None, 'client_lead')."""
    lowered = text.lower()
    for niche_key in ("seo_mkt", "web_dev", "ai_auto", "admin_growth"):
        keywords = NICHES.get(niche_key, {}).get("keywords", [])
        if any(kw.lower() in lowered for kw in keywords):
            category = "agency_prospect" if niche_key == "admin_growth" else "client_lead"
            return niche_key, category
    for q in DISTRIBUTOR_QUERIES:
        if q.lower() in lowered:
            return "admin_growth", "distributor_prospect"
    return None, "client_lead"


def _strip_html(raw: str) -> str:
    """Remove basic HTML tags from Hacker News Algolia snippets."""
    return re.sub(r"<[^>]+>", " ", raw).strip()


def _extract_post_id(url: str) -> str:
    match = _COMMENT_ID_RE.search(url)
    return f"t3_{match.group(1)}" if match else ""


def _extract_subreddit(url: str) -> str:
    match = _SUBREDDIT_RE.search(url)
    return match.group(1) if match else ""


def _normalise_old_reddit_url(raw_href: str) -> str:
    if raw_href.startswith("/r/"):
        return OLD_REDDIT_BASE + raw_href
    if raw_href.startswith("https://www.reddit.com"):
        return raw_href.replace("https://www.reddit.com", OLD_REDDIT_BASE, 1)
    return raw_href


def _fetch_post_body(page: Page, old_reddit_url: str) -> str:
    if not old_reddit_url.startswith("http"):
        return ""
    try:
        page.goto(old_reddit_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(1_000)
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
# Live Diagnostic Test Scrape (/test_scrape — All 5 Platforms)
# ---------------------------------------------------------------------------

def _build_fallback_outreach(post: dict, matched_kw: str) -> tuple[str, str]:
    """Return (suggested_comment, suggested_dm) for test cards when Gemini isn't used."""
    title = (post.get("title") or "your post").strip()[:80]
    kw = matched_kw or "this"
    comment = (
        f"Great question on {kw}! A quick win here is auditing the current funnel/setup first "
        f"so you don't waste budget on low-converting traffic. Happy to share a quick breakdown "
        f"or checklist if that helps."
    )
    dm = (
        f"Hey! Saw your post about \"{title}\" — I've helped similar teams solve {kw} "
        f"without the usual agency fluff. Happy to send over a 2-minute teardown of what I'd fix first, "
        f"no strings attached."
    )
    return comment, dm


def _enrich_test_match(match: dict, niche: str) -> dict:
    """Attach description snippet + suggested_comment + custom_dm to a /test_scrape match."""
    desc = " ".join((match.get("selftext") or match.get("title") or "").split())
    if len(desc) > 360:
        desc = desc[:357].rstrip() + "..."
    match["description"] = desc or "(no description)"

    comment, dm = _build_fallback_outreach(match, match.get("matched_keyword", ""))
    try:
        from config import GEMINI_API_KEY
        if GEMINI_API_KEY:
            from ai_filter import qualify_lead
            q = qualify_lead(
                {
                    "platform": match.get("platform", "Reddit"),
                    "group_or_sub": match.get("group_or_sub", ""),
                    "niche": niche,
                    "category": "client_lead",
                    "title": match.get("title", ""),
                    "selftext": match.get("selftext", ""),
                }
            )
            if q:
                comment = q.get("suggested_comment") or comment
                dm = q.get("custom_dm") or dm
    except Exception as exc:
        logger.debug("Test match Gemini enrichment skipped: %s", exc)

    match["suggested_comment"] = comment
    match["custom_dm"] = dm
    return match


def run_test_scrape(niche: str = "seo_mkt") -> dict:
    """
    Test all 5 platforms (Reddit, Hacker News, Indie Hackers, Product Hunt, X)
    in one quick cycle without mutating `leads.db`. Returns per-platform status,
    posts scanned, keyword matches found, and enriched top sample matches.
    """
    cfg = NICHES.get(niche) or NICHES.get("seo_mkt", {})
    keywords = cfg.get("keywords", ["seo", "marketing"])
    subreddits = (cfg.get("subreddits") or ["SEO", "marketing"])[:2]

    platform_reports: list[dict] = []
    matches: list[dict] = []
    total_scanned = 0

    try:
        # 1. Hacker News (requests, no browser)
        hn_status: int | str | None = None
        hn_scanned = 0
        hn_matches = 0
        hn_err: str | None = None
        try:
            unix_24h_ago = int(time.time()) - 86_400
            resp = requests.get(
                HN_API_URL,
                params={
                    "tags": "(story,comment,ask_hn)",
                    "numericFilters": f"created_at_i>{unix_24h_ago}",
                    "hitsPerPage": 50,
                },
                headers={"User-Agent": STEALTH_USER_AGENT},
                timeout=10,
            )
            hn_status = resp.status_code
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
            hn_scanned = len(hits)
            total_scanned += hn_scanned
            for hit in hits:
                title = (hit.get("title") or hit.get("story_title") or "").strip()
                body = _strip_html(hit.get("comment_text") or hit.get("story_text") or "")
                combined = f"{title} {body}".lower()
                matched_kw = next((kw for kw in keywords if kw.lower() in combined), None)
                if matched_kw:
                    hn_matches += 1
                    obj_id = hit.get("objectID", "")
                    tags = hit.get("_tags") or []
                    group = "Ask HN" if "ask_hn" in tags else "Hacker News"
                    matches.append(
                        {
                            "platform": "Hacker News",
                            "group_or_sub": group,
                            "title": title or body[:120],
                            "selftext": body or title,
                            "url": f"https://news.ycombinator.com/item?id={obj_id}",
                            "matched_keyword": matched_kw,
                        }
                    )
        except Exception as exc:
            hn_err = str(exc)

        platform_reports.append(
            {
                "platform": "Hacker News",
                "badge": "🟧 Hacker News",
                "status": hn_status,
                "posts_scanned": hn_scanned,
                "matches_found": hn_matches,
                "error": hn_err,
            }
        )

        # 2-5. Playwright platforms with stealth context, asset blocking & 15s timeout
        with sync_playwright() as pw:
            browser: Browser = _launch_browser(pw)
            try:
                context = _new_stealth_context(browser)
                page: Page = _new_fast_page(context)

                # 2. Reddit (old.reddit.com/r/{sub}/new.json + HTML fallback)
                rd_status: int | None = None
                rd_scanned = 0
                rd_matches = 0
                rd_err: str | None = None
                for sub in subreddits:
                    posts, sub_status, sub_err = _fetch_reddit_subreddit_posts(page, sub)
                    if sub_status:
                        rd_status = sub_status
                    if sub_err and not posts:
                        rd_err = sub_err
                        continue
                    rd_scanned += len(posts)
                    total_scanned += len(posts)
                    for item in posts:
                        combined = f"{item['title']} {item['selftext']}".lower()
                        matched_kw = next(
                            (kw for kw in keywords if kw.lower() in combined), None
                        )
                        if matched_kw:
                            rd_matches += 1
                            matches.append(
                                {
                                    "platform": "Reddit",
                                    "group_or_sub": f"r/{sub}",
                                    "title": item["title"],
                                    "selftext": item["selftext"] or item["title"],
                                    "url": item["url"],
                                    "matched_keyword": matched_kw,
                                }
                            )

                platform_reports.append(
                    {
                        "platform": "Reddit",
                        "badge": f"🟠 Reddit ({', '.join('r/' + s for s in subreddits)})",
                        "status": rd_status,
                        "posts_scanned": rd_scanned,
                        "matches_found": rd_matches,
                        "error": rd_err,
                    }
                )

                # 3. Indie Hackers
                ih_items, ih_status, ih_err = _fetch_indiehackers_items(page, INDIEHACKERS_NEWEST_URL)
                ih_scanned = len(ih_items)
                total_scanned += ih_scanned
                ih_matches = 0
                for item in ih_items:
                    combined = f"{item['title']} {item['body']}".lower()
                    matched_kw = next((kw for kw in keywords if kw.lower() in combined), None)
                    if matched_kw:
                        ih_matches += 1
                        matches.append(
                            {
                                "platform": "Indie Hackers",
                                "group_or_sub": "Indie Hackers",
                                "title": item["title"],
                                "selftext": item["body"] or item["title"],
                                "url": item["url"],
                                "matched_keyword": matched_kw,
                            }
                        )
                platform_reports.append(
                    {
                        "platform": "Indie Hackers",
                        "badge": "🔵 Indie Hackers",
                        "status": ih_status,
                        "posts_scanned": ih_scanned,
                        "matches_found": ih_matches,
                        "error": ih_err,
                    }
                )

                # 4. Product Hunt
                ph_items, ph_status, ph_err = _fetch_producthunt_items(
                    page, PRODUCTHUNT_DISCUSSIONS_URL
                )
                ph_scanned = len(ph_items)
                total_scanned += ph_scanned
                ph_matches = 0
                for item in ph_items:
                    combined = f"{item['title']} {item['body']}".lower()
                    matched_kw = next((kw for kw in keywords if kw.lower() in combined), None)
                    if matched_kw:
                        ph_matches += 1
                        matches.append(
                            {
                                "platform": "Product Hunt",
                                "group_or_sub": "Product Hunt",
                                "title": item["title"],
                                "selftext": item["body"] or item["title"],
                                "url": item["url"],
                                "matched_keyword": matched_kw,
                            }
                        )
                platform_reports.append(
                    {
                        "platform": "Product Hunt",
                        "badge": "🚀 Product Hunt",
                        "status": ph_status,
                        "posts_scanned": ph_scanned,
                        "matches_found": ph_matches,
                        "error": ph_err,
                    }
                )

                # 5. X / Twitter & Joined Communities
                auth_token, ct0 = _get_x_credentials()
                if not auth_token or not ct0:
                    platform_reports.append(
                        {
                            "platform": "X",
                            "badge": "𝕏 X / Community",
                            "status": "Skipped",
                            "posts_scanned": 0,
                            "matches_found": 0,
                            "error": "X_AUTH_TOKEN / X_CT0 not set",
                        }
                    )
                else:
                    _inject_x_cookies(context, auth_token, ct0)
                    q_entry = next(
                        (q for q in X_SEARCH_QUERIES if q["niche"] == niche),
                        X_SEARCH_QUERIES[0],
                    )
                    x_items, x_status, x_err = _fetch_x_tweets(
                        page, X_SEARCH_URL.format(query=quote_plus(q_entry["query"]))
                    )
                    c_items, c_status, c_err = _fetch_x_tweets(page, X_COMMUNITIES_URL)
                    all_x = [("X Search", t) for t in x_items] + [("X Community", t) for t in c_items]
                    x_scanned = len(all_x)
                    total_scanned += x_scanned
                    x_matches = 0
                    for grp, tw in all_x:
                        matched_kw = next(
                            (kw for kw in keywords if kw.lower() in tw["text"].lower()), None
                        )
                        if matched_kw:
                            x_matches += 1
                            matches.append(
                                {
                                    "platform": "X",
                                    "group_or_sub": grp,
                                    "title": tw["text"][:140],
                                    "selftext": tw["text"],
                                    "url": tw["url"],
                                    "matched_keyword": matched_kw,
                                }
                            )
                    platform_reports.append(
                        {
                            "platform": "X",
                            "badge": "𝕏 X / Community",
                            "status": x_status or c_status,
                            "posts_scanned": x_scanned,
                            "matches_found": x_matches,
                            "error": x_err or c_err,
                        }
                    )
            finally:
                browser.close()

        top_matches = [_enrich_test_match(m, niche) for m in matches[:3]]
        return {
            "niche": niche,
            "keywords": keywords,
            "details": platform_reports,
            "total_scanned": total_scanned,
            "matches": top_matches,
            "total_matches": len(matches),
            "error": None,
        }
    except Exception as exc:
        logger.error("[TEST_SCRAPE] Fatal error: %s", exc)
        return {
            "niche": niche,
            "keywords": keywords,
            "details": platform_reports,
            "total_scanned": total_scanned,
            "matches": [],
            "total_matches": 0,
            "error": str(exc),
        }

"""
scraper.py — Multi-Platform Global Harvester (Reddit Dual Multireddit RSS, Product Hunt Feed,
Hacker News Algolia API, Lightweight Playwright Indie Hackers, and Auto-Skipping X).

Combines five scraping engines into `scrape_all_platforms()`:
  1. `scrape_hackernews_api()`        — Fast JSON API via hn.algolia.com (last 24h, with author profile URL)
  2. `scrape_reddit_global()`         — Dual multireddit RSS feeds with 2s delay & browser User-Agent
  3. `scrape_producthunt()`           — Product Hunt public feed (producthunt.com/feed)
  4. `scrape_indiehackers(page)`      — Playwright on indiehackers.com/newest + post body/author hydration
  5. `scrape_x_global(context, page)` — Optional cookie-authenticated X search & communities with auto-skip

Global Playwright Speed & RAM Optimization:
  - Blocks `image`, `stylesheet`, `font`, and `media` requests via `page.route("**/*", ...)`.
  - Uses `wait_until="domcontentloaded"` with `timeout=15000` everywhere.
  - Always closes page, context, and browser in `finally:` blocks to avoid Chromium zombies.
"""

import hashlib
import html
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
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
    DISTRIBUTOR_QUERIES,
    GEMINI_API_KEY,
    GROQ_API_KEY,
    HN_API_URL,
    INDIEHACKERS_NEWEST_URL,
    NICHES,
    PLAYWRIGHT_TIMEOUT_MS,
    PRODUCTHUNT_FEED_URL,
    REDDIT_RSS_URLS,
    USER_AGENT,
    X_AUTH_TOKEN,
    X_CT0,
    X_SEARCH_QUERIES,
)
from database import is_post_processed, mark_post_processed

logger = logging.getLogger(__name__)

REDDIT_RSS_DELAY_SECONDS = 2
REDDIT_RSS_UA = USER_AGENT

STEALTH_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

X_HOME_URL = "https://x.com/home"
X_SEARCH_URL = "https://x.com/search?q={query}&f=live"
X_COMMUNITIES_URL = "https://x.com/i/communities"

MAX_POSTS_PER_SUB = 25
NAV_TIMEOUT_MS = PLAYWRIGHT_TIMEOUT_MS  # 15000 ms
SETTLE_WAIT_MS = 1_500
BLOCKED_RESOURCE_TYPES = {"image", "stylesheet", "font", "media"}

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
_IH_RESERVED_SLUGS = {
    "post",
    "posts",
    "groups",
    "group",
    "newest",
    "top",
    "product",
    "products",
    "start",
    "about",
    "login",
    "signup",
    "settings",
    "search",
    "newsletter",
    "podcast",
}


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


def _should_attempt_x() -> tuple[bool, str]:
    """Return (should_run, skip_reason) for optional X / Communities scraping."""
    flag = os.getenv("ENABLE_X_SCRAPER", "").strip().lower()
    if flag in ("false", "0", "no"):
        return False, "disabled via ENABLE_X_SCRAPER"
    auth_token, ct0 = _get_x_credentials()
    if not auth_token or not ct0:
        return False, "X_AUTH_TOKEN / X_CT0 not set"
    return True, ""


def scrape_all_platforms(active_niches: set[str] | None = None) -> list[dict]:
    """
    Run all platform harvesters (Hacker News, dual Reddit multireddit RSS,
    Product Hunt Feed, Indie Hackers, and auto-skipping X) and return candidate leads.
    """
    leads: list[dict] = []

    # 1. Fast HTTP/API harvesters (no browser needed)
    for name, fn in (
        ("Hacker News", scrape_hackernews_api),
        ("Reddit RSS", scrape_reddit_global),
        ("Product Hunt Feed", scrape_producthunt),
    ):
        try:
            leads.extend(fn(active_niches=active_niches))
        except Exception as exc:
            logger.warning("%s scraper failed: %s", name, exc)

    # Check X availability up front so cookies-missing logs once at INFO cleanly
    x_should_run, x_skip_reason = _should_attempt_x()
    if not x_should_run:
        logger.info("X scraper skipped cleanly (%s).", x_skip_reason)

    # 2. Browser-based harvesters (Indie Hackers + optional X) with strict cleanup
    with sync_playwright() as pw:
        browser: Browser = _launch_browser(pw)
        context: BrowserContext | None = None
        page: Page | None = None
        try:
            context = _new_stealth_context(browser)
            page = _new_fast_page(context)

            try:
                leads.extend(scrape_indiehackers(page, active_niches=active_niches))
            except Exception as exc:
                logger.warning("Indie Hackers scraper failed: %s", exc)

            if x_should_run:
                try:
                    leads.extend(scrape_x_global(context, page, active_niches=active_niches))
                except Exception as exc:
                    logger.info("X scraper auto-skipped due to error: %s", exc)
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            try:
                browser.close()
            except Exception:
                pass

    logger.info("Multi-platform harvest complete. %d candidate lead(s) queued.", len(leads))
    return leads


# Backward-compatible alias
scrape_subreddits = scrape_all_platforms


# ---------------------------------------------------------------------------
# 1. Reddit Dual Multireddit Public RSS Harvester (2 light requests + 2s delay)
# ---------------------------------------------------------------------------

def _parse_reddit_rss_xml(xml_bytes: bytes) -> list[dict]:
    """Parse Atom entries from a Reddit multireddit RSS response."""
    root = ET.fromstring(xml_bytes)
    posts: list[dict] = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        post_id = (entry.findtext("atom:id", default="", namespaces=_ATOM_NS) or "").strip()
        title = html.unescape(
            (entry.findtext("atom:title", default="", namespaces=_ATOM_NS) or "").strip()
        )
        raw_content = entry.findtext("atom:content", default="", namespaces=_ATOM_NS) or ""
        selftext = _clean_reddit_rss_content(raw_content)

        link_el = entry.find("atom:link", _ATOM_NS)
        post_url = (link_el.attrib.get("href", "") if link_el is not None else "").strip()

        cat_el = entry.find("atom:category", _ATOM_NS)
        subreddit = (cat_el.attrib.get("term", "") if cat_el is not None else "").strip()
        if not subreddit and post_url:
            m = re.search(r"/r/([^/]+)/", post_url, re.IGNORECASE)
            subreddit = m.group(1) if m else "reddit"

        raw_author = (
            entry.findtext("atom:author/atom:name", default="", namespaces=_ATOM_NS) or ""
        ).strip()
        author = raw_author.removeprefix("/u/").removeprefix("u/").strip()
        author_url = f"https://www.reddit.com/user/{author}" if author else ""

        if post_id and title:
            posts.append(
                {
                    "post_id": post_id,
                    "subreddit": subreddit or "reddit",
                    "title": title,
                    "selftext": selftext,
                    "url": post_url,
                    "author": author,
                    "author_url": author_url,
                }
            )
    return posts


def _fetch_combined_reddit_rss() -> tuple[list[dict], int | None, str | None]:
    """
    Fetch target subreddits across two lightweight multireddit RSS URLs with a
    2-second delay and realistic browser User-Agent, returning deduplicated posts.
    """
    headers = {
        "User-Agent": REDDIT_RSS_UA,
        "Accept": "application/atom+xml, application/xml, text/xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    all_posts: list[dict] = []
    seen_ids: set[str] = set()
    last_status: int | None = None
    errors: list[str] = []

    for idx, rss_url in enumerate(REDDIT_RSS_URLS):
        if idx > 0:
            time.sleep(REDDIT_RSS_DELAY_SECONDS)
        resp = None
        try:
            resp = requests.get(rss_url, headers=headers, timeout=15)
            last_status = resp.status_code
            resp.raise_for_status()
            parsed = _parse_reddit_rss_xml(resp.content)
            for item in parsed:
                if item["post_id"] not in seen_ids:
                    seen_ids.add(item["post_id"])
                    all_posts.append(item)
        except Exception as exc:
            err_status = getattr(resp, "status_code", None)
            if err_status is not None:
                last_status = err_status
            errors.append(f"feed {idx + 1}: {exc}")

    err_msg = "; ".join(errors) if (errors and not all_posts) else None
    if errors and all_posts:
        logger.warning("Partial Reddit RSS warning (%s); %d posts still retrieved.", "; ".join(errors), len(all_posts))
    return all_posts, last_status, err_msg


def _clean_reddit_rss_content(raw_html: str) -> str:
    """Extract clean post body text from Reddit RSS `<content>` HTML."""
    unescaped = html.unescape(raw_html)
    unescaped = re.sub(r"<!--\s*SC_OFF\s*-->", "", unescaped)
    unescaped = re.sub(r"<!--\s*SC_ON\s*-->.*$", "", unescaped, flags=re.DOTALL)
    text = _strip_html(unescaped)
    return " ".join(text.split())


def scrape_reddit_global(active_niches: set[str] | None = None) -> list[dict]:
    """Scrape target subreddits via dual multireddit RSS requests."""
    posts, status, err = _fetch_combined_reddit_rss()
    if err:
        logger.warning("Reddit RSS failed (%s): %s", status, err)
        return []

    logger.info("Reddit Dual RSS -> HTTP %s | %d posts", status, len(posts))
    leads: list[dict] = []

    for item in posts:
        post_id = item["post_id"]
        if is_post_processed(post_id):
            continue

        subreddit = item["subreddit"]
        combined = f"{item['title']} {item['selftext']}"

        # Prefer niches that explicitly track this subreddit, otherwise check any niche
        preferred_niches = [
            k for k, cfg in NICHES.items()
            if any(s.lower() == subreddit.lower() for s in cfg.get("subreddits", []))
        ]
        candidate_niches = preferred_niches or list(NICHES.keys())
        all_matched = [
            nk for nk in candidate_niches
            if _matches_keywords(combined, NICHES[nk].get("keywords", []))
        ]
        if not all_matched:
            mark_post_processed(post_id, "")
            continue

        if active_niches is not None:
            matched_niches = [nk for nk in all_matched if nk in active_niches]
        else:
            matched_niches = all_matched

        if not matched_niches:
            continue

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
                    "selftext": item["selftext"][:2000],
                    "url": item["url"],
                    "author": item["author"],
                    "author_url": item["author_url"],
                }
            )

    return leads


# ---------------------------------------------------------------------------
# 2. Hacker News Live API Harvester
# ---------------------------------------------------------------------------

def scrape_hackernews_api(active_niches: set[str] | None = None) -> list[dict]:
    """
    Fetch real-time stories & comments via hn.algolia.com/api/v1/search_by_date
    for the last 24h using `requests` (no browser needed), including author HN profile URL.
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

        niche, category = _classify_text_to_niche(combined_text, allowed_niches=active_niches)
        if not niche:
            if active_niches is None or _classify_text_to_niche(combined_text)[0] is None:
                mark_post_processed(post_id, "")
            continue

        tags = hit.get("_tags") or []
        if "ask_hn" in tags or title.lower().startswith("ask hn"):
            group_or_sub = "Ask HN"
        elif "comment" in tags:
            group_or_sub = "HN Comment"
        else:
            group_or_sub = "Hacker News"

        author = (hit.get("author") or "").strip()
        author_url = f"https://news.ycombinator.com/user?id={author}" if author else ""
        hn_url = f"https://news.ycombinator.com/item?id={object_id}"

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
                "author": author,
                "author_url": author_url,
            }
        )

    return leads


# ---------------------------------------------------------------------------
# 3. Product Hunt Public Feed Harvester (no 403)
# ---------------------------------------------------------------------------

def _fetch_producthunt_feed_items() -> tuple[list[dict], int | None, str | None]:
    """
    Fetch https://www.producthunt.com/feed via requests + XML parsing to capture
    newly launched products, taglines, descriptions, and maker links without 403 blocks.
    """
    headers = {
        "User-Agent": REDDIT_RSS_UA,
        "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml",
    }
    resp = None
    try:
        resp = requests.get(PRODUCTHUNT_FEED_URL, headers=headers, timeout=12)
        status = resp.status_code
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as exc:
        return [], getattr(resp, "status_code", None), str(exc)

    items: list[dict] = []

    # Atom format (<entry>)
    entries = root.findall("atom:entry", _ATOM_NS)
    if entries:
        for entry in entries[:30]:
            title = html.unescape(
                (entry.findtext("atom:title", default="", namespaces=_ATOM_NS) or "").strip()
            )
            raw_body = (
                entry.findtext("atom:content", default="", namespaces=_ATOM_NS)
                or entry.findtext("atom:summary", default="", namespaces=_ATOM_NS)
                or ""
            )
            body = " ".join(_strip_html(html.unescape(raw_body)).split())
            link_el = entry.find("atom:link", _ATOM_NS)
            url = (link_el.attrib.get("href", "") if link_el is not None else "").strip()
            author = (
                entry.findtext("atom:author/atom:name", default="", namespaces=_ATOM_NS) or ""
            ).strip()
            author_uri = (
                entry.findtext("atom:author/atom:uri", default="", namespaces=_ATOM_NS) or ""
            ).strip()
            if title and url:
                items.append(
                    {
                        "title": title,
                        "body": body[:2000],
                        "url": url,
                        "author": author,
                        "author_url": author_uri,
                    }
                )
        return items, status, None

    # RSS 2.0 fallback (<channel>/<item>)
    for item in root.findall("./channel/item")[:30]:
        title = html.unescape((item.findtext("title") or "").strip())
        raw_body = item.findtext("description") or ""
        body = " ".join(_strip_html(html.unescape(raw_body)).split())
        url = (item.findtext("link") or "").strip()
        author = (item.findtext("author") or "").strip()
        if title and url:
            items.append(
                {
                    "title": title,
                    "body": body[:2000],
                    "url": url,
                    "author": author,
                    "author_url": "",
                }
            )
    return items, status, None


def scrape_producthunt(active_niches: set[str] | None = None) -> list[dict]:
    """Harvest Product Hunt launches/descriptions via the public XML feed."""
    leads: list[dict] = []
    items, status, err = _fetch_producthunt_feed_items()
    if err:
        logger.warning("Product Hunt feed error (%s): %s", status, err)
        return []

    logger.info("Product Hunt Feed -> HTTP %s | %d items", status, len(items))
    for item in items:
        post_id = "ph_" + hashlib.sha1(item["url"].encode("utf-8")).hexdigest()[:12]
        if is_post_processed(post_id):
            continue

        combined = f"{item['title']} {item['body']}"
        niche, category = _classify_text_to_niche(combined, allowed_niches=active_niches)
        if not niche:
            if active_niches is None or _classify_text_to_niche(combined)[0] is None:
                mark_post_processed(post_id, "")
            continue

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
                "author": item.get("author", ""),
                "author_url": item.get("author_url", ""),
            }
        )

    return leads


# ---------------------------------------------------------------------------
# 4. Indie Hackers Harvester (Playwright + Post Body & Author Hydration)
# ---------------------------------------------------------------------------

def scrape_indiehackers(page: Page, active_niches: set[str] | None = None) -> list[dict]:
    """Scrape indiehackers.com/newest (main feed), hydrating full post body & author profile."""
    leads: list[dict] = []
    url, group_label = INDIEHACKERS_NEWEST_URL, "Indie Hackers"

    items, status, err = _fetch_indiehackers_items(page, url)
    if err:
        logger.warning("Indie Hackers (%s) error: %s", url, err)
        return []
    logger.info("Indie Hackers (%s) -> HTTP %s | %d items", group_label, status, len(items))

    hydrated = 0
    for item in items:
        post_id = "ih_" + hashlib.sha1(item["url"].encode("utf-8")).hexdigest()[:12]
        if is_post_processed(post_id):
            continue

        combined = f"{item['title']} {item['body']}"
        niche, category = _classify_text_to_niche(combined, allowed_niches=active_niches)
        if not niche:
            if active_niches is None or _classify_text_to_niche(combined)[0] is None:
                mark_post_processed(post_id, "")
            continue

        body, author, author_url = ("", "", "")
        if hydrated < 12:
            body, author, author_url = _hydrate_indiehackers_post(page, item["url"], item["title"])
            hydrated += 1
        final_body = body or item["body"]
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
                "selftext": final_body[:2000],
                "url": item["url"],
                "author": author,
                "author_url": author_url,
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


def _hydrate_indiehackers_post(page: Page, post_url: str, title: str) -> tuple[str, str, str]:
    """
    Visit an Indie Hackers post URL to extract the actual post body (excluding
    repeated title/upvotes) and the author's profile URL (`indiehackers.com/{username}`).
    """
    try:
        page.goto(post_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(SETTLE_WAIT_MS)
    except Exception:
        return "", "", ""

    body = ""
    for sel in (".post-page__body", ".content", "article .body", "main p"):
        els = page.query_selector_all(sel)
        if els:
            joined = " ".join((e.inner_text() or "").strip() for e in els[:6]).strip()
            if len(joined) > 25 and joined.lower() != title.lower():
                body = joined
                break

    author = ""
    author_url = ""
    for a in page.query_selector_all('a[href^="/"]')[:30]:
        href = (a.get_attribute("href") or "").strip()
        if re.fullmatch(r"/[A-Za-z0-9_.-]{2,32}", href):
            slug = href.lstrip("/")
            if slug.lower() not in _IH_RESERVED_SLUGS:
                author = slug
                author_url = f"https://www.indiehackers.com/{slug}"
                break

    return body[:2000], author, author_url


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


def _warm_x_session(page: Page) -> tuple[int | None, str | None]:
    """
    Load https://x.com/home first after setting auth_token/ct0 so X sets session
    cookies (`twid`/`guest_id`) before navigating to search/communities.
    """
    try:
        resp = page.goto(X_HOME_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        status = resp.status if resp else None
        page.wait_for_timeout(2_000)
        if status == 403:
            return status, "X blocked datacenter IP — requires residential proxy or fresh cookies"
        if "/login" in page.url or "/i/flow/login" in page.url:
            return status, "X blocked datacenter IP — requires residential proxy or fresh cookies"
        return status, None
    except PWTimeout:
        return None, "Timeout loading x.com/home"
    except Exception as exc:
        return None, str(exc)


def scrape_x_global(
    context: BrowserContext,
    page: Page,
    active_niches: set[str] | None = None,
) -> list[dict]:
    """
    Inject X_AUTH_TOKEN / X_CT0 cookies, warm session on x.com/home, then scan
    live X search queries and joined X Communities (`x.com/i/communities`).
    Auto-skips cleanly with a single info log if cookies are missing or blocked.
    """
    auth_token, ct0 = _get_x_credentials()
    if not auth_token or not ct0:
        logger.info("X scraper skipped cleanly: X_AUTH_TOKEN / X_CT0 not set.")
        return []

    _inject_x_cookies(context, auth_token, ct0)
    _, warm_err = _warm_x_session(page)
    if warm_err:
        logger.info("X scraper auto-skipped: %s", warm_err)
        return []

    leads: list[dict] = []

    # 5a. Joined X Communities timeline
    comm_items, status, err = _fetch_x_tweets(page, X_COMMUNITIES_URL)
    if err:
        logger.info("X scraper auto-skipped on Communities: %s", err)
        if any(tok in err.lower() for tok in ("blocked", "expired", "403", "login")):
            return []
    else:
        logger.info("X Communities -> HTTP %s | %d tweets", status, len(comm_items))
        for tw in comm_items:
            post_id = "x_" + hashlib.sha1(tw["url"].encode("utf-8")).hexdigest()[:12]
            if is_post_processed(post_id):
                continue

            niche, category = _classify_text_to_niche(tw["text"], allowed_niches=active_niches)
            if not niche:
                continue

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
                    "author": tw.get("author", ""),
                    "author_url": tw.get("author_url", ""),
                }
            )

    # 5b. High-intent live X search queries (only queries relevant to active_niches)
    for entry in X_SEARCH_QUERIES:
        if active_niches is not None and entry["niche"] not in active_niches:
            continue
        search_url = X_SEARCH_URL.format(query=quote_plus(entry["query"]))
        tweets, status, err = _fetch_x_tweets(page, search_url)
        if err:
            logger.info("X scraper auto-skipped on Search (%s): %s", entry["niche"], err)
            if any(tok in err.lower() for tok in ("blocked", "expired", "403", "login")):
                break
            continue

        logger.info("X Search [%s] -> HTTP %s | %d tweets", entry["niche"], status, len(tweets))
        for tw in tweets:
            post_id = "x_" + hashlib.sha1(tw["url"].encode("utf-8")).hexdigest()[:12]
            if is_post_processed(post_id):
                continue

            niche, category = _classify_text_to_niche(tw["text"], allowed_niches=active_niches)
            resolved_niche = niche or entry["niche"]
            resolved_cat = category if niche else "client_lead"

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
                    "author": tw.get("author", ""),
                    "author_url": tw.get("author_url", ""),
                }
            )

    return leads


def _fetch_x_tweets(page: Page, url: str) -> tuple[list[dict], int | None, str | None]:
    """Navigate to an X timeline/search page, wait 3s for tweets to render, and extract text + URL."""
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        status = resp.status if resp else None
        if status == 403:
            return [], status, "X blocked datacenter IP — requires residential proxy or fresh cookies"
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
        return [], status, "X blocked datacenter IP — requires residential proxy or fresh cookies"

    articles = page.query_selector_all('article[data-testid="tweet"]')
    if not articles:
        login_input = page.query_selector('input[name="text"]')
        if login_input:
            return [], status, "X blocked datacenter IP — requires residential proxy or fresh cookies"
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

            handle_match = re.search(r"x\.com/([^/]+)/status/", tweet_url)
            author = handle_match.group(1) if handle_match else ""
            author_url = f"https://x.com/{author}" if author else ""

            tweets.append({"text": text, "url": tweet_url, "author": author, "author_url": author_url})
        except Exception:
            continue

    return tweets, status, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _classify_text_to_niche(
    text: str,
    allowed_niches: set[str] | None = None,
) -> tuple[str | None, str]:
    """Return (niche_key, category) matching *allowed_niches* (or any niche if None)."""
    lowered = text.lower()
    for niche_key in ("seo_mkt", "web_dev", "ai_auto", "admin_growth"):
        if allowed_niches is not None and niche_key not in allowed_niches:
            continue
        keywords = NICHES.get(niche_key, {}).get("keywords", [])
        if any(kw.lower() in lowered for kw in keywords):
            category = "agency_prospect" if niche_key == "admin_growth" else "client_lead"
            return niche_key, category
    if allowed_niches is None or "admin_growth" in allowed_niches:
        for q in DISTRIBUTOR_QUERIES:
            if q.lower() in lowered:
                return "admin_growth", "distributor_prospect"
    return None, "client_lead"


def _strip_html(raw: str) -> str:
    """Remove HTML tags from RSS/HN snippets."""
    return re.sub(r"<[^>]+>", " ", raw).strip()


def _matches_keywords(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(kw.lower() in lowered for kw in keywords)


# ---------------------------------------------------------------------------
# Live Diagnostic Test Scrape (/test_scrape — All 5 Platforms)
# ---------------------------------------------------------------------------

def _enrich_test_match(match: dict, niche: str) -> dict | None:
    """
    Attach description snippet and AI-generated friendly public comment + DM
    to a `/test_scrape` match. Filters out posts rejected as non-buyers (`is_buyer=False`).
    """
    desc = " ".join((match.get("selftext") or match.get("title") or "").split())
    if len(desc) > 400:
        desc = desc[:397].rstrip() + "..."
    match["description"] = desc or "(no description)"
    match["suggested_comment"] = ""
    match["custom_dm"] = ""

    has_ai_key = bool(
        (os.getenv("GROQ_API_KEY") or GROQ_API_KEY)
        or (os.getenv("GEMINI_API_KEY") or GEMINI_API_KEY)
    )
    if not has_ai_key:
        return match

    try:
        from ai_filter import qualify_lead

        q = qualify_lead(
            {
                "platform": match.get("platform", "Reddit"),
                "group_or_sub": match.get("group_or_sub", ""),
                "niche": niche,
                "category": "client_lead",
                "author": match.get("author", ""),
                "title": match.get("title", ""),
                "selftext": match.get("selftext", ""),
            }
        )
        if q:
            if not q.get("is_buyer"):
                return None
            match["intent_score"] = q.get("intent_score", 0)
            match["suggested_comment"] = q.get("suggested_comment", "")
            match["custom_dm"] = q.get("custom_dm", "")
    except Exception as exc:
        logger.debug("Test match AI enrichment failed: %s", exc)

    return match


def run_test_scrape(niche: str = "seo_mkt") -> dict:
    """
    Test all 5 platforms (Reddit RSS, Hacker News, Product Hunt Feed, Indie Hackers, X)
    in one quick cycle without mutating `leads.db`. Returns per-platform status,
    posts scanned, keyword matches found, and Gemini-enriched top matches.
    """
    cfg = NICHES.get(niche) or NICHES.get("seo_mkt", {})
    keywords = cfg.get("keywords", ["seo", "marketing"])
    subreddits = (cfg.get("subreddits") or ["SEO", "marketing"])[:2]

    platform_reports: list[dict] = []
    raw_matches: list[dict] = []
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
                headers={"User-Agent": USER_AGENT},
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
                    author = (hit.get("author") or "").strip()
                    tags = hit.get("_tags") or []
                    group = "Ask HN" if "ask_hn" in tags else "Hacker News"
                    raw_matches.append(
                        {
                            "platform": "Hacker News",
                            "group_or_sub": group,
                            "title": title or body[:120],
                            "selftext": body or title,
                            "url": f"https://news.ycombinator.com/item?id={obj_id}",
                            "author": author,
                            "author_url": f"https://news.ycombinator.com/user?id={author}" if author else "",
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

        # 2. Combined Reddit RSS (1 single HTTP request, zero 429)
        posts, rd_status, rd_err = _fetch_combined_reddit_rss()
        rd_scanned = len(posts)
        total_scanned += rd_scanned
        rd_matches = 0
        for item in posts:
            combined = f"{item['title']} {item['selftext']}".lower()
            matched_kw = next((kw for kw in keywords if kw.lower() in combined), None)
            if matched_kw:
                rd_matches += 1
                raw_matches.append(
                    {
                        "platform": "Reddit",
                        "group_or_sub": f"r/{item['subreddit']}",
                        "title": item["title"],
                        "selftext": item["selftext"] or item["title"],
                        "url": item["url"],
                        "author": item["author"],
                        "author_url": item["author_url"],
                        "matched_keyword": matched_kw,
                    }
                )

        platform_reports.append(
            {
                "platform": "Reddit",
                "badge": "🟠 Reddit (Dual RSS)",
                "status": rd_status,
                "posts_scanned": rd_scanned,
                "matches_found": rd_matches,
                "error": rd_err,
            }
        )

        # 3. Product Hunt Feed (requests, no 403)
        ph_items, ph_status, ph_err = _fetch_producthunt_feed_items()
        ph_scanned = len(ph_items)
        total_scanned += ph_scanned
        ph_matches = 0
        for item in ph_items:
            combined = f"{item['title']} {item['body']}".lower()
            matched_kw = next((kw for kw in keywords if kw.lower() in combined), None)
            if matched_kw:
                ph_matches += 1
                raw_matches.append(
                    {
                        "platform": "Product Hunt",
                        "group_or_sub": "Product Hunt",
                        "title": item["title"],
                        "selftext": item["body"] or item["title"],
                        "url": item["url"],
                        "author": item.get("author", ""),
                        "author_url": item.get("author_url", ""),
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

        # 4 & 5. Playwright platforms (Indie Hackers & optional X)
        with sync_playwright() as pw:
            browser: Browser = _launch_browser(pw)
            context: BrowserContext | None = None
            page: Page | None = None
            try:
                context = _new_stealth_context(browser)
                page = _new_fast_page(context)

                # 4. Indie Hackers (+ hydrate post body & author profile for matched posts)
                ih_items, ih_status, ih_err = _fetch_indiehackers_items(page, INDIEHACKERS_NEWEST_URL)
                ih_scanned = len(ih_items)
                total_scanned += ih_scanned
                ih_matches = 0
                for item in ih_items:
                    combined = f"{item['title']} {item['body']}".lower()
                    matched_kw = next((kw for kw in keywords if kw.lower() in combined), None)
                    if matched_kw:
                        ih_matches += 1
                        body, author, author_url = _hydrate_indiehackers_post(
                            page, item["url"], item["title"]
                        )
                        raw_matches.append(
                            {
                                "platform": "Indie Hackers",
                                "group_or_sub": "Indie Hackers",
                                "title": item["title"],
                                "selftext": body or item["body"] or item["title"],
                                "url": item["url"],
                                "author": author,
                                "author_url": author_url,
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

                # 5. X / Twitter & Joined Communities (cookie-optional, auto-skips cleanly)
                x_should_run, x_skip_reason = _should_attempt_x()
                if not x_should_run:
                    platform_reports.append(
                        {
                            "platform": "X",
                            "badge": "𝕏 X / Community",
                            "status": "Skipped",
                            "posts_scanned": 0,
                            "matches_found": 0,
                            "error": x_skip_reason,
                        }
                    )
                else:
                    auth_token, ct0 = _get_x_credentials()
                    _inject_x_cookies(context, auth_token, ct0)
                    warm_status, warm_err = _warm_x_session(page)
                    if warm_err:
                        platform_reports.append(
                            {
                                "platform": "X",
                                "badge": "𝕏 X / Community",
                                "status": "Skipped",
                                "posts_scanned": 0,
                                "matches_found": 0,
                                "error": warm_err,
                            }
                        )
                    else:
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
                                raw_matches.append(
                                    {
                                        "platform": "X",
                                        "group_or_sub": grp,
                                        "title": tw["text"][:140],
                                        "selftext": tw["text"],
                                        "url": tw["url"],
                                        "author": tw.get("author", ""),
                                        "author_url": tw.get("author_url", ""),
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
                if page is not None:
                    try:
                        page.close()
                    except Exception:
                        pass
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                try:
                    browser.close()
                except Exception:
                    pass

        qualified_matches: list[dict] = []
        for m in raw_matches[:8]:
            enriched = _enrich_test_match(m, niche)
            if enriched is not None:
                qualified_matches.append(enriched)
            if len(qualified_matches) >= 3:
                break

        return {
            "niche": niche,
            "keywords": keywords,
            "details": platform_reports,
            "total_scanned": total_scanned,
            "matches": qualified_matches,
            "total_matches": len(raw_matches),
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

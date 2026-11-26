"""
config.py — Central configuration for the Multi-Tenant SaaS Intent Lead Bot.
Defines credentials, niche-to-subreddit/keyword mappings, and thresholds.
"""

import os
from dotenv import load_dotenv

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Credentials (loaded from .env)
# ---------------------------------------------------------------------------
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID: str = os.getenv("ADMIN_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL_MINUTES: int = int(os.getenv("CHECK_INTERVAL_MINUTES", "15"))
DODO_PAYMENT_LINK: str = os.getenv(
    "DODO_PAYMENT_LINK",
    "https://checkout.dodopayments.com/buy/your_product_link_here",
)
BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")
DAILY_OUTREACH_TARGET: int = 50

# ---------------------------------------------------------------------------
# Niche & Admin Growth Mappings
# ---------------------------------------------------------------------------
NICHES: dict[str, dict[str, list[str]]] = {
    "web_dev": {
        "subreddits": ["SaaS", "startups", "Entrepreneur", "forhire", "freelance_forhire"],
        "keywords": [
            "looking for developer",
            "looking for a dev",
            "need a developer",
            "looking to hire",
            "build an mvp",
            "need help building",
            "need a website",
            "hire a dev",
            "recommend a",
            "anyone know a good",
        ],
    },
    "ai_auto": {
        "subreddits": ["Automate", "smallbusiness", "Entrepreneur", "SaaS"],
        "keywords": [
            "ai automation",
            "automate workflow",
            "automate my",
            "looking for agency",
            "need ai agent",
            "zapier expert",
            "looking to hire",
            "recommend a",
            "anyone know a good",
        ],
    },
    "seo_mkt": {
        "subreddits": ["smallbusiness", "Entrepreneur", "marketing", "forhire"],
        "keywords": [
            "need seo",
            "looking for marketing agency",
            "google ads help",
            "need lead generation",
            "looking to hire",
            "recommend a",
            "anyone know a good",
        ],
    },
    "admin_growth": {
        "subreddits": ["agency", "freelance", "webdev", "SEO", "marketing"],
        "keywords": [
            "how to get clients",
            "struggling to find clients",
            "cold email not working",
            "need more leads",
            "first agency client",
        ],
    },
}

NICHE_LABELS: dict[str, str] = {
    "web_dev": "💻 Web & MVP Dev",
    "ai_auto": "🤖 AI Automation",
    "seo_mkt": "📈 SEO & Marketing",
    "admin_growth": "🔥 Admin Growth",
}

# Combined list of all active intent keywords across niches
INTENT_KEYWORDS: list[str] = [
    kw
    for cfg in NICHES.values()
    for kw in cfg.get("keywords", [])
]

# ---------------------------------------------------------------------------
# Global 50/Day Multi-Platform Prospecting Queries
# ---------------------------------------------------------------------------
ADMIN_GLOBAL_QUERIES: list[str] = [
    "how to get web dev clients",
    "struggling to find agency clients",
    "cold email not working",
    "need more B2B leads",
    "looking for lead generation",
    "best way to find SaaS clients",
]

DISTRIBUTOR_QUERIES: list[str] = [
    "looking for micro saas to partner",
    "affiliate marketing for B2B saas",
    "agency growth consultant",
    "looking for recurring commission",
]

WEB_SOCIAL_QUERIES: list[dict[str, str]] = [
    {
        "platform": "X / Twitter",
        "query": 'site:x.com "looking for clients" OR "struggling to get clients"',
    },
    {
        "platform": "IndieHackers",
        "query": 'site:indiehackers.com "need customers" OR "first 10 customers"',
    },
]

HN_API_URL: str = "https://hn.algolia.com/api/v1/search_by_date"
DDG_HTML_URL: str = "https://html.duckduckgo.com/html/"

# ---------------------------------------------------------------------------
# HTTP / Playwright Browser Settings
# ---------------------------------------------------------------------------
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# SaaS Trial & Qualification Thresholds
# ---------------------------------------------------------------------------
TRIAL_DAYS: int = 5
MIN_INTENT_SCORE: int = 7

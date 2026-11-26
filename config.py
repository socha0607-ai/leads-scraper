"""
config.py — Central configuration for the Multi-Tenant SaaS Intent Lead Bot.
Defines credentials, niche-to-subreddit/keyword mappings, and thresholds.
"""

import os
from dotenv import load_dotenv

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Credentials & Runtime Settings (loaded from .env)
# ---------------------------------------------------------------------------
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "").strip()
GROQ_API_URL: str = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip() or "llama-3.3-70b-versatile"

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip() or "gemini-3.8-flash"
GEMINI_FALLBACK_MODELS: list[str] = [
    "gemini-3.8-flash",
    "gemini-3.1-pro-preview",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
]

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT_ID: str = os.getenv("ADMIN_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "")
DB_PATH: str = os.getenv("DB_PATH", "/data/leads.db").strip() or "/data/leads.db"

CHECK_INTERVAL_MINUTES: int = int(os.getenv("CHECK_INTERVAL_MINUTES", "15"))
CHECK_INTERVAL_SECONDS: int = int(
    os.getenv("CHECK_INTERVAL_SECONDS", str(CHECK_INTERVAL_MINUTES * 60))
)
STARTUP_DELAY_SECONDS: int = int(os.getenv("STARTUP_DELAY_SECONDS", "6"))
LEAD_RETENTION_DAYS: int = int(os.getenv("LEAD_RETENTION_DAYS", "14"))

ENABLE_X_SCRAPER: bool = os.getenv("ENABLE_X_SCRAPER", "").strip().lower() == "true"
DODO_PAYMENT_LINK: str = os.getenv(
    "DODO_PAYMENT_LINK",
    "https://checkout.dodopayments.com/buy/your_product_link_here",
)
BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")
DAILY_OUTREACH_TARGET: int = 50

# Optional X (Twitter) session cookies for authenticated search & joined Communities
X_AUTH_TOKEN: str = os.getenv("X_AUTH_TOKEN", "").strip()
X_CT0: str = os.getenv("X_CT0", "").strip()

# ---------------------------------------------------------------------------
# Niche & Admin Growth Mappings
# ---------------------------------------------------------------------------
NICHES: dict[str, dict[str, list[str]]] = {
    "web_dev": {
        "subreddits": [
            "SaaS",
            "startups",
            "Entrepreneur",
            "forhire",
            "freelance_forhire",
            "webdev",
        ],
        "keywords": [
            "mvp",
            "developer",
            "build an app",
            "build a website",
            "web app",
            "saas",
            "landing page",
            "shopify",
            "wordpress",
            "react",
            "next.js",
            "freelancer",
            "agency",
            "prototype",
            "co-founder",
            "ui/ux",
        ],
    },
    "ai_auto": {
        "subreddits": [
            "Automate",
            "smallbusiness",
            "Entrepreneur",
            "SaaS",
            "n8n",
            "zapier",
        ],
        "keywords": [
            "automate",
            "automation",
            "ai agent",
            "chatbot",
            "n8n",
            "make.com",
            "zapier",
            "workflow",
            "scraper",
            "scraping",
            "voice ai",
            "openai",
            "api integration",
            "manual work",
            "crm",
        ],
    },
    "seo_mkt": {
        "subreddits": [
            "SEO",
            "digital_marketing",
            "marketing",
            "smallbusiness",
            "Entrepreneur",
            "SaaS",
            "PPC",
            "freelance",
        ],
        "keywords": [
            "seo",
            "backlinks",
            "google ads",
            "facebook ads",
            "meta ads",
            "more clients",
            "get clients",
            "lead gen",
            "lead generation",
            "marketing",
            "organic traffic",
            "google ranking",
            "search traffic",
            "agency",
            "freelancer",
            "ad spend",
            "roas",
            "local seo",
            "audit",
            "conversion rate",
        ],
    },
    "admin_growth": {
        "subreddits": ["agency", "freelance", "webdev", "SEO", "marketing"],
        "keywords": [
            "need seo",
            "looking for marketing agency",
            "marketing",
            "how to get clients",
            "struggling to find clients",
            "cold email not working",
            "need more leads",
            "first agency client",
            "more clients",
            "get clients",
            "lead generation",
        ],
    },
}

NICHE_LABELS: dict[str, str] = {
    "web_dev": "💻 Web & MVP Dev",
    "ai_auto": "🤖 AI Automation",
    "seo_mkt": "📈 SEO & Marketing",
    "admin_growth": "🔥 Admin Growth",
}

NICHE_DESCRIPTIONS: dict[str, str] = {
    "web_dev": "Full-stack web apps, SaaS MVPs, landing pages, Shopify/WordPress, React/Next.js & UI/UX requests.",
    "ai_auto": "Workflow automation, AI agents, chatbots, n8n/Make.com/Zapier builds, scraping & CRM integrations.",
    "seo_mkt": "SEO audits, backlinks, Google/Meta ads, local SEO, organic traffic, ROAS & client growth requests.",
    "admin_growth": "Agency growth, client acquisition, lead generation & B2B partnership prospects.",
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

X_SEARCH_QUERIES: list[dict[str, str]] = [
    {
        "niche": "seo_mkt",
        "query": '"need seo" OR "looking for seo" OR "google ads" OR "more clients" -filter:replies',
    },
    {
        "niche": "web_dev",
        "query": '"need a developer" OR "build an mvp" OR "build a website" OR "looking for react" -filter:replies',
    },
    {
        "niche": "ai_auto",
        "query": '"automate workflow" OR "ai agent" OR "n8n" OR "zapier" "looking for" -filter:replies',
    },
]

REDDIT_RSS_URLS: list[str] = [
    "https://www.reddit.com/r/SEO+digital_marketing+marketing+smallbusiness/new/.rss?limit=25",
    "https://www.reddit.com/r/Entrepreneur+forhire+webdev+freelance/new/.rss?limit=25",
]
REDDIT_COMBINED_RSS_URL: str = os.getenv("REDDIT_COMBINED_RSS_URL", REDDIT_RSS_URLS[0])
HN_API_URL: str = "https://hn.algolia.com/api/v1/search_by_date"
INDIEHACKERS_NEWEST_URL: str = "https://www.indiehackers.com/newest"
PRODUCTHUNT_FEED_URL: str = "https://www.producthunt.com/feed"
DDG_HTML_URL: str = "https://html.duckduckgo.com/html/"

# ---------------------------------------------------------------------------
# HTTP / Playwright Browser Settings
# ---------------------------------------------------------------------------
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
PLAYWRIGHT_TIMEOUT_MS: int = 15_000

# ---------------------------------------------------------------------------
# SaaS Trial & Qualification Thresholds
# ---------------------------------------------------------------------------
TRIAL_DAYS: int = 5
MIN_INTENT_SCORE: int = int(os.getenv("MIN_INTENT_SCORE", "7"))

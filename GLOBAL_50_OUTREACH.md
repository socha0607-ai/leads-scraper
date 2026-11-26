# Project Upgrade: Global 50/Day Multi-Platform Outreach & Distributor Engine

## 🎯 Objective
Upgrade `/media/admin01/02427D50427D4985/intent-lead-scraper` into a high-volume **Global Multi-Platform Prospecting Engine** capable of surfacing **50+ high-intent prospects/day** (both Agency Buyers AND Global Distributors/Affiliates) across high-traffic global platforms, complete with tailored pitches and live sample lead links.

## 🛠️ Required Upgrades

### 1. Update `config.py` (Global Platforms, High-Traffic Queries & Daily Cap)
Add global prospecting configurations:
- `DAILY_OUTREACH_TARGET = 50`
- **Global Buyer & Agency Prospecting Queries (`ADMIN_GLOBAL_QUERIES`):**
  - `"how to get web dev clients"`
  - `"struggling to find agency clients"`
  - `"cold email not working"`
  - `"need more B2B leads"`
  - `"looking for lead generation"`
  - `"best way to find SaaS clients"`
- **Global Distributor / Partner Queries (`DISTRIBUTOR_QUERIES`):**
  - `"looking for micro saas to partner"`
  - `"affiliate marketing for B2B saas"`
  - `"agency growth consultant"`
  - `"looking for recurring commission"`
- **Global Platform Endpoints:**
  - Reddit Global Search: `https://old.reddit.com/search?q={query}&sort=new&t=day`
  - Hacker News Live API: `https://hn.algolia.com/api/v1/search_by_date?query={query}&tags=(story,comment)&numericFilters=created_at_i>{unix_24h_ago}`
  - Web/X/IndieHackers Discovery (via DuckDuckGo HTML `https://html.duckduckgo.com/html/?q={query}`):
    - `site:x.com "looking for clients" OR "struggling to get clients"`
    - `site:indiehackers.com "need customers" OR "first 10 customers"`

### 2. Upgrade `scraper.py` (Multi-Platform Global Harvester)
Implement three clean scraping functions combined into `scrape_all_platforms()`:
1. **`scrape_reddit_global(page)`**: Uses Playwright on `old.reddit.com/search` for all niche + admin + distributor queries (`t=day`), extracting up to 15 fresh posts per query.
2. **`scrape_hackernews_api()`**: Queries `hn.algolia.com/api/v1/search_by_date` via `requests` (fast, zero-block JSON) for the last 24 hours across agency/SaaS lead-gen queries.
3. **`scrape_web_social_global(page)`**: Uses Playwright on DuckDuckGo HTML (`html.duckduckgo.com/html/`) to extract direct links and snippets from `x.com` and `indiehackers.com` posted recently.
- Deduplicate all URLs/post_ids against `leads.db` (`processed_posts` table).
- Tag each item with `platform` (`Reddit`, `HackerNews`, `X / Twitter`, `IndieHackers`) and `category` (`client_lead`, `agency_prospect`, or `distributor_prospect`).

### 3. Upgrade `ai_filter.py` (Sample Lead Pairing + Ready-to-Send Pitch)
Update Gemini 2.5 Flash prompt logic:
- Maintain a rolling cache of the **latest 3 verified `client_lead` URLs** from `leads.db` (fresh buyers looking for developers/agencies).
- When qualifying an **`agency_prospect`**:
  - Generate `custom_dm`: A 3-sentence non-spammy pitch that references their exact post AND embeds 1–2 real sample lead URLs from the cache as proof of value, ending with a soft invite to start the 5-day free trial on `https://t.me/Lead_Scraper01_bot` (or dynamic `BOT_USERNAME` from `.env`).
- When qualifying a **`distributor_prospect`**:
  - Generate `custom_dm`: A partnership pitch offering **30% recurring lifetime commission** (`$24/mo per active user`) to promote the Intent Lead Scraper bot to their agency/freelancer audience.

### 4. Update `database.py` & `notifier.py` (Daily 50-Lead Counter & Clean Formatting)
- Add helper `get_today_admin_outreach_count()` in `database.py` to track how many outreach prospects were sent to `ADMIN_CHAT_ID` today (target: `50/day`).
- Format every Telegram alert sent to `ADMIN_CHAT_ID` cleanly:
  ```text
  🚀 GLOBAL OUTREACH PROSPECT [{today_count}/50 Today]
  🌐 Platform: {platform} | 🎯 Type: {category}
  📝 Context: {summary}
  🔗 Direct Profile/Post Link: {url}

  📋 COPY-PASTE PITCH (Includes Live Sample Proof):
  {custom_dm}
  ```

### 5. Verification
- Run `venv/bin/python -m py_compile *.py` and ensure zero syntax errors.
- Test `scrape_hackernews_api()` and `scrape_reddit_global()` in a dry run to confirm live global results are fetched.

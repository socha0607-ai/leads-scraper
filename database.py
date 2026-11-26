"""
database.py — SQLite persistence for SaaS users, trials, and post deduplication.

Tables:
  - processed_posts (post_id TEXT PRIMARY KEY, niche TEXT, created_at TIMESTAMP)
  - users (chat_id INTEGER PRIMARY KEY, username TEXT, niche TEXT,
           plan TEXT DEFAULT 'trial', trial_ends_at TIMESTAMP, created_at TIMESTAMP)
"""

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import DB_PATH as DEFAULT_DB_PATH, LEAD_RETENTION_DAYS, TRIAL_DAYS

logger = logging.getLogger(__name__)


def _resolve_initial_db_path() -> Path:
    """Resolve persistent SQLite DB path (defaults to /data/leads.db on Railway)."""
    raw = (os.getenv("DB_PATH") or DEFAULT_DB_PATH or "/data/leads.db").strip()
    candidate = Path(raw)
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate
    except OSError as exc:
        fallback = Path(__file__).parent / "leads.db"
        logger.warning(
            "Cannot write to %s (%s) — falling back to local SQLite %s.",
            candidate,
            exc,
            fallback,
        )
        fallback.parent.mkdir(parents=True, exist_ok=True)
        return fallback


DB_PATH: Path = _resolve_initial_db_path()


def _get_connection() -> sqlite3.Connection:
    """Return a SQLite connection configured for multi-thread access with WAL."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    """Create tables if missing and migrate legacy processed_posts schema if needed."""
    with _get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_posts (
                post_id    TEXT PRIMARY KEY,
                niche      TEXT DEFAULT '',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Migrate legacy leads.db created before the SaaS upgrade (had `subreddit` instead of `niche`).
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(processed_posts)").fetchall()
        }
        if "niche" not in cols:
            conn.execute("ALTER TABLE processed_posts ADD COLUMN niche TEXT DEFAULT ''")
            logger.info("Migrated processed_posts table: added `niche` column.")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id       INTEGER PRIMARY KEY,
                username      TEXT DEFAULT '',
                niche         TEXT NOT NULL,
                plan          TEXT NOT NULL DEFAULT 'trial',
                trial_ends_at TIMESTAMP NOT NULL,
                created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        user_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "updated_at" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN updated_at TIMESTAMP DEFAULT ''")
            logger.info("Migrated users table: added `updated_at` column.")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS verified_client_leads (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                url        TEXT UNIQUE NOT NULL,
                niche      TEXT DEFAULT '',
                title      TEXT DEFAULT '',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_outreach_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id      TEXT UNIQUE NOT NULL,
                category     TEXT NOT NULL,
                platform     TEXT NOT NULL,
                created_date TEXT NOT NULL,
                created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    logger.info("Database initialised at %s", DB_PATH)


# ---------------------------------------------------------------------------
# Post Deduplication
# ---------------------------------------------------------------------------

def is_post_processed(post_id: str) -> bool:
    """Return True if *post_id* has already been processed."""
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_posts WHERE post_id = ?", (post_id,)
        ).fetchone()
    return row is not None


def mark_post_processed(post_id: str, niche: str = "") -> None:
    """Record *post_id* as processed (idempotent)."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_connection() as conn:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(processed_posts)").fetchall()
        }
        if "subreddit" in cols:
            conn.execute(
                """
                INSERT OR IGNORE INTO processed_posts (post_id, subreddit, niche, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (post_id, niche, niche, now),
            )
        else:
            conn.execute(
                """
                INSERT OR IGNORE INTO processed_posts (post_id, niche, created_at)
                VALUES (?, ?, ?)
                """,
                (post_id, niche, now),
            )
        conn.commit()
    logger.debug("Marked post %s (niche=%s) as processed.", post_id, niche)


# ---------------------------------------------------------------------------
# SaaS User & Trial Management
# ---------------------------------------------------------------------------

def register_or_update_niche(chat_id: int, username: str, niche: str) -> dict:
    """
    Register a new user with a 5-day free trial, or update an existing user's niche.
    Returns a dict with user info and `is_new` flag.
    """
    now = datetime.now(timezone.utc)
    clean_username = username or ""

    with _get_connection() as conn:
        existing = conn.execute(
            "SELECT chat_id, plan, trial_ends_at FROM users WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()

        if existing is None:
            now_iso = now.isoformat()
            trial_ends = (now + timedelta(days=TRIAL_DAYS)).isoformat()
            conn.execute(
                """
                INSERT INTO users (chat_id, username, niche, plan, trial_ends_at, created_at, updated_at)
                VALUES (?, ?, ?, 'trial', ?, ?, ?)
                """,
                (chat_id, clean_username, niche, trial_ends, now_iso, now_iso),
            )
            conn.commit()
            logger.info("Registered new trial user chat_id=%s (@%s) for niche=%s", chat_id, clean_username, niche)
            return {"chat_id": chat_id, "niche": niche, "plan": "trial", "is_new": True}

        conn.execute(
            """
            UPDATE users
            SET niche = ?, username = ?, updated_at = ?
            WHERE chat_id = ?
            """,
            (niche, clean_username, now.isoformat(), chat_id),
        )
        conn.commit()
        logger.info("Updated niche for chat_id=%s to %s", chat_id, niche)
        return {
            "chat_id": chat_id,
            "niche": niche,
            "plan": existing["plan"],
            "is_new": False,
        }


def _is_user_active(row: sqlite3.Row, now: datetime) -> bool:
    """Return True if a users row holds an active Pro plan or unexpired Trial."""
    plan = row["plan"]
    ends_at = _parse_utc(row["trial_ends_at"])
    if plan == "pro":
        return ends_at is None or ends_at > now
    if plan == "trial":
        return ends_at is not None and ends_at > now
    return False


def get_active_users() -> list[dict]:
    """Return active user dicts (valid trial or pro) with chat_id, username, niche, plan."""
    now = datetime.now(timezone.utc)
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT chat_id, username, niche, plan, trial_ends_at FROM users"
        ).fetchall()

    active: list[dict] = []
    for row in rows:
        if _is_user_active(row, now):
            active.append(
                {
                    "chat_id": row["chat_id"],
                    "username": row["username"],
                    "niche": row["niche"],
                    "plan": row["plan"],
                }
            )
    return active


def get_active_user_ids() -> list[int]:
    """Return chat_ids of all users whose plan is currently active."""
    return [u["chat_id"] for u in get_active_users()]


def get_active_niches() -> set[str]:
    """Return distinct niche keys currently tracked by at least one active user."""
    return {u["niche"] for u in get_active_users() if u.get("niche")}


def prune_old_posts(days: int = LEAD_RETENTION_DAYS) -> int:
    """Delete seen-lead records in processed_posts older than *days* (default 14)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stale_ids: list[str] = []

    with _get_connection() as conn:
        rows = conn.execute("SELECT post_id, created_at FROM processed_posts").fetchall()
        for row in rows:
            ts = _parse_utc(row["created_at"])
            if ts and ts < cutoff:
                stale_ids.append(row["post_id"])

        if stale_ids:
            conn.executemany(
                "DELETE FROM processed_posts WHERE post_id = ?",
                [(pid,) for pid in stale_ids],
            )
            conn.commit()

    if stale_ids:
        logger.info("Pruned %d seen-lead record(s) older than %d day(s).", len(stale_ids), days)
    return len(stale_ids)


def get_active_users_by_niche(niche: str) -> list[int]:
    """
    Return chat_ids subscribed to *niche* whose plan is active ('pro' or unexpired 'trial').
    """
    now = datetime.now(timezone.utc)
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT chat_id, plan, trial_ends_at FROM users WHERE niche = ?",
            (niche,),
        ).fetchall()

    return [row["chat_id"] for row in rows if _is_user_active(row, now)]


def get_user_status(chat_id: int) -> dict:
    """
    Return exact subscription/trial status and remaining time for *chat_id*.
    Keys: exists, niche, plan, is_active, days_left, hours_left.
    """
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT chat_id, username, niche, plan, trial_ends_at, created_at FROM users WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()

    if row is None:
        return {
            "exists": False,
            "niche": "",
            "plan": "",
            "is_active": False,
            "days_left": 0,
            "hours_left": 0,
        }

    now = datetime.now(timezone.utc)
    ends_at = _parse_utc(row["trial_ends_at"])
    total_seconds = (ends_at - now).total_seconds() if ends_at else 0.0

    if total_seconds > 0:
        days_left = int(total_seconds // 86400)
        hours_left = int((total_seconds % 86400) // 3600)
    else:
        days_left = 0
        hours_left = 0

    plan = row["plan"]
    is_active = (plan == "pro" and (ends_at is None or total_seconds > 0)) or (
        plan == "trial" and total_seconds > 0
    )

    return {
        "exists": True,
        "chat_id": row["chat_id"],
        "username": row["username"],
        "niche": row["niche"],
        "plan": plan,
        "trial_ends_at": row["trial_ends_at"],
        "is_active": is_active,
        "days_left": days_left,
        "hours_left": hours_left,
    }


def upgrade_user_to_pro(chat_id: int, days: int = 30) -> bool:
    """Upgrade *chat_id* to 'pro' and extend expiry by *days*. Returns True if updated."""
    now = datetime.now(timezone.utc)
    new_expiry = (now + timedelta(days=days)).isoformat()

    with _get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE users
            SET plan = 'pro', trial_ends_at = ?
            WHERE chat_id = ?
            """,
            (new_expiry, chat_id),
        )
        conn.commit()

    if cur.rowcount == 0:
        logger.warning("upgrade_user_to_pro: chat_id=%s not found in users table.", chat_id)
        return False

    logger.info("Upgraded chat_id=%s to PRO for %d days (until %s).", chat_id, days, new_expiry)
    return True


def _parse_utc(ts: str | None) -> datetime | None:
    """Parse an ISO or SQLite timestamp string into a timezone-aware UTC datetime."""
    if not ts:
        return None
    normalized = ts.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Sample Client Lead Cache & Daily Admin Outreach Counter
# ---------------------------------------------------------------------------

def save_verified_client_lead(url: str, niche: str, title: str) -> None:
    """Store a verified buyer lead URL for sample proof in admin outreach pitches."""
    if not url:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO verified_client_leads (url, niche, title, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (url, niche, title[:200], now),
        )
        conn.commit()


def get_latest_client_lead_urls(limit: int = 3) -> list[str]:
    """Return the latest *limit* verified client_lead URLs from leads.db."""
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT url FROM verified_client_leads ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [row["url"] for row in rows if row["url"]]


def get_today_admin_outreach_count() -> int:
    """Return how many outreach prospects were sent to ADMIN_CHAT_ID today (UTC)."""
    today_str = datetime.now(timezone.utc).date().isoformat()
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM admin_outreach_log WHERE created_date = ?",
            (today_str,),
        ).fetchone()
    return int(row["cnt"]) if row else 0


def record_admin_outreach(post_id: str, category: str, platform: str) -> int:
    """Record a delivered admin outreach prospect and return today's updated count."""
    now = datetime.now(timezone.utc)
    today_str = now.date().isoformat()
    with _get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO admin_outreach_log (post_id, category, platform, created_date, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (post_id, category, platform, today_str, now.isoformat()),
        )
        conn.commit()
    return get_today_admin_outreach_count()

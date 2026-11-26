"""
main.py — Durable low-RAM runner for the Multi-Platform Intent Lead Radar SaaS Bot.

Runs:
  1. Interactive Telegram bot polling (`start_polling`) after a 6s deploy grace delay.
  2. Every 900s (15 min): active-user gate -> multi-platform scrape -> 50-lead batch AI
     qualification (Groq primary, Gemini backup) -> Telegram delivery -> 14-day DB prune -> gc.collect().
"""

import gc
import logging
import sys
import threading
import time

import schedule
from dotenv import load_dotenv

load_dotenv(override=True)

from config import (  # noqa: E402
    CHECK_INTERVAL_SECONDS,
    LEAD_RETENTION_DAYS,
    MIN_INTENT_SCORE,
    STARTUP_DELAY_SECONDS,
)
from database import (  # noqa: E402
    get_active_users,
    init_db,
    mark_post_processed,
    prune_old_posts,
    save_verified_client_lead,
)
from bot_handler import get_bot_username, start_polling  # noqa: E402
from scraper import scrape_all_platforms  # noqa: E402
from ai_filter import MAX_BATCH_CANDIDATES, prefilter_candidates, qualify_leads_batch  # noqa: E402
from notifier import send_lead_alert  # noqa: E402


class _FlushingStreamHandler(logging.StreamHandler):
    """StreamHandler that flushes immediately so Docker/Railway logs appear in real time."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[_FlushingStreamHandler(sys.stdout)],
)
logger = logging.getLogger("main")


def _run_bot_polling() -> None:
    logger.info("Telegram bot polling thread started.")
    start_polling()


def run_pipeline() -> None:
    """Execute one zero-waste 15-minute scrape → batch qualify → deliver → prune → GC cycle."""
    print("=" * 60, flush=True)
    logger.info("Global multi-platform pipeline run started.")

    try:
        active_users = get_active_users()
        active_niches = {u["niche"] for u in active_users if u.get("niche")}
        if not active_users or not active_niches:
            logger.info("Zero active users — skipping cycle to save resources")
            prune_old_posts(days=LEAD_RETENTION_DAYS)
            return

        logger.info(
            "Active users: %d | Tracking niches: %s",
            len(active_users),
            ", ".join(sorted(active_niches)),
        )

        leads = scrape_all_platforms(active_niches=active_niches)
        if not leads:
            logger.info("No new keyword-matched leads found this cycle.")
            prune_old_posts(days=LEAD_RETENTION_DAYS)
            return

        candidates = prefilter_candidates(
            leads,
            active_niches=active_niches,
            max_candidates=MAX_BATCH_CANDIDATES,
        )
        # If under batch cap, remaining leads had zero buyer signals so mark them seen cleanly.
        if len(candidates) < MAX_BATCH_CANDIDATES:
            candidate_ids = {str(c.get("post_id") or "") for c in candidates}
            for post in leads:
                pid = str(post.get("post_id") or "")
                if pid and pid not in candidate_ids:
                    mark_post_processed(pid, "")

        if not candidates:
            logger.info("No unseen buyer-signal candidates passed local pre-filter.")
            prune_old_posts(days=LEAD_RETENTION_DAYS)
            return

        bot_username = get_bot_username()
        logger.info(
            "Batch-qualifying %d candidate(s) in ONE AI call…",
            len(candidates),
        )
        qualifications = qualify_leads_batch(
            candidates,
            bot_username=bot_username,
            active_niches=active_niches,
            already_prefiltered=True,
        )

        total_deliveries = 0
        for post in candidates:
            post_id = str(post.get("post_id", ""))
            niche = str(post.get("niche", ""))
            if post_id:
                mark_post_processed(post_id, niche)

            qualification = qualifications.get(post_id)
            if not qualification:
                continue

            if (
                post.get("category") == "client_lead"
                and qualification.get("is_buyer")
                and int(qualification.get("intent_score", 0)) >= MIN_INTENT_SCORE
            ):
                save_verified_client_lead(
                    url=post.get("url", ""),
                    niche=niche,
                    title=post.get("title", ""),
                )

            try:
                sent_count = send_lead_alert(post, qualification)
                total_deliveries += sent_count
            except Exception as exc:
                logger.error("Notification error for %s: %s", post_id, exc)

        prune_old_posts(days=LEAD_RETENTION_DAYS)
        logger.info(
            "Pipeline run complete. Total alert deliveries: %d across %d qualified lead(s).",
            total_deliveries,
            len(qualifications),
        )
    except Exception as exc:
        logger.error("Pipeline cycle failed with an unexpected error: %s", exc)
    finally:
        gc.collect()
        logger.debug("Cycle garbage collection complete.")


def main() -> None:
    logger.info("Global 50/Day Intent Lead SaaS Bot starting up…")
    init_db()
    prune_old_posts(days=LEAD_RETENTION_DAYS)

    if STARTUP_DELAY_SECONDS > 0:
        logger.info(
            "Waiting %ds before starting Telegram polling (avoiding 409 Conflict on rolling deploy)…",
            STARTUP_DELAY_SECONDS,
        )
        time.sleep(STARTUP_DELAY_SECONDS)

    polling_thread = threading.Thread(
        target=_run_bot_polling,
        name="telegram-polling",
        daemon=True,
    )
    polling_thread.start()

    run_pipeline()
    schedule.every(CHECK_INTERVAL_SECONDS).seconds.do(run_pipeline)
    logger.info(
        "Scheduler active. Next run in %d second(s) (%d min). Press Ctrl+C to stop.",
        CHECK_INTERVAL_SECONDS,
        CHECK_INTERVAL_SECONDS // 60,
    )

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except KeyboardInterrupt:
            logger.info("Shutting down gracefully. Goodbye!")
            break
        except Exception as exc:
            logger.error("Unexpected error in scheduler loop: %s", exc)
            time.sleep(60)


if __name__ == "__main__":
    main()

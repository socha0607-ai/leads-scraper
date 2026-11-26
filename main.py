"""
main.py — Concurrent runner for the Global 50/Day Multi-Platform SaaS Lead Bot.

Runs:
  1. Interactive Telegram bot polling (`start_polling`) in a daemon thread.
  2. Multi-platform harvester (`scrape_all_platforms`) + Gemini 2.5 Flash
     qualification + rolling sample lead caching + niche/admin alert routing.
"""

import logging
import sys
import threading
import time

import schedule
from dotenv import load_dotenv

load_dotenv(override=True)

from config import CHECK_INTERVAL_MINUTES, MIN_INTENT_SCORE  # noqa: E402
from database import init_db, save_verified_client_lead
from bot_handler import get_bot_username, start_polling
from scraper import scrape_all_platforms
from ai_filter import qualify_lead
from notifier import send_lead_alert


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
    """Execute one full multi-platform scrape → qualify → cache/route cycle."""
    print("=" * 60, flush=True)
    logger.info("Global multi-platform pipeline run started.")

    try:
        leads = scrape_all_platforms()
    except Exception as exc:
        logger.error("Scraping failed with an unexpected error: %s", exc)
        return

    if not leads:
        logger.info("No new keyword-matched leads found this cycle.")
        return

    bot_username = get_bot_username()
    logger.info("Qualifying %d candidate(s) with Gemini…", len(leads))

    total_deliveries = 0
    for post in leads:
        post_id = post.get("post_id", "")
        try:
            qualification = qualify_lead(post, bot_username=bot_username)
        except Exception as exc:
            logger.error("AI qualification error for %s: %s", post_id, exc)
            continue

        if qualification is None:
            continue

        # Cache verified client_lead URLs so admin outreach pitches include live sample proof
        if (
            post.get("category") == "client_lead"
            and qualification.get("is_buyer")
            and int(qualification.get("intent_score", 0)) >= MIN_INTENT_SCORE
        ):
            save_verified_client_lead(
                url=post.get("url", ""),
                niche=post.get("niche", ""),
                title=post.get("title", ""),
            )

        try:
            sent_count = send_lead_alert(post, qualification)
            total_deliveries += sent_count
        except Exception as exc:
            logger.error("Notification error for %s: %s", post_id, exc)

    logger.info(
        "Pipeline run complete. Total alert deliveries: %d across %d candidates.",
        total_deliveries,
        len(leads),
    )


def main() -> None:
    logger.info("Global 50/Day Intent Lead SaaS Bot starting up…")
    init_db()

    polling_thread = threading.Thread(
        target=_run_bot_polling,
        name="telegram-polling",
        daemon=True,
    )
    polling_thread.start()

    run_pipeline()
    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(run_pipeline)
    logger.info(
        "Scheduler active. Next run in %d minute(s). Press Ctrl+C to stop.",
        CHECK_INTERVAL_MINUTES,
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

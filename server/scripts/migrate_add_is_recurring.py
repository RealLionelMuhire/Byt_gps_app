import sys
import os
import logging

# Match the pattern used by cron_expiry.py — add server root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy import text
from app.core.database import SessionLocal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def run():
    db = SessionLocal()
    try:
        # IF NOT EXISTS makes this safe to run multiple times.
        # Default False: all existing subscriptions are treated as one-time
        # (they expire after their duration). Admin or renewal logic sets
        # is_recurring=True for plans that auto-renew.
        db.execute(text(
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS "
            "is_recurring BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        db.commit()
        logger.info("✅  Migration complete: subscriptions.is_recurring column ready.")
    except Exception as e:
        db.rollback()
        logger.error("❌  Migration failed: %s", e)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    run()

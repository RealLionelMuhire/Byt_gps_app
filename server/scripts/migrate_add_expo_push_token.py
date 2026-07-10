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
        # IF NOT EXISTS makes this safe to run multiple times
        db.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS expo_push_token VARCHAR(255) NULL"
        ))
        db.commit()
        logger.info("✅  Migration complete: expo_push_token column ready.")
    except Exception as e:
        db.rollback()
        logger.error("❌  Migration failed: %s", e)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    run()

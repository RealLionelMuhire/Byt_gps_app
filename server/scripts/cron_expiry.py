import sys
import os
from datetime import datetime

# Add the server directory to sys.path so we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.database import SessionLocal
from app.models.subscription import Subscription
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def check_expired_subscriptions():
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        expired_subs = db.query(Subscription).filter(
            Subscription.status == "active",
            Subscription.expires_at < now
        ).all()

        if not expired_subs:
            logger.info("No expired subscriptions found.")
            return

        for sub in expired_subs:
            logger.info(f"Expiring subscription for user {sub.clerk_user_id} (plan {sub.plan_id})")
            sub.status = "expired"
            sub.updated_at = datetime.utcnow()
            
            # Optional: send push notification via FCM here.
            # send_fcm_notification(sub.clerk_user_id, "Plan expired", "Renew your plan to continue tracking your vehicles.", {"screen": "plan_upgrade"})

        db.commit()
        logger.info(f"Successfully expired {len(expired_subs)} subscriptions.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error while checking expired subscriptions: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    check_expired_subscriptions()

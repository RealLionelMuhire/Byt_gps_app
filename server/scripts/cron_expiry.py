import asyncio
import sys
import os
from datetime import datetime, timedelta

# Add the server directory to sys.path so we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.subscription import Subscription, Payment
from app.services.intouchpay import get_transaction_status, classify_status, IntouchPayError
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Reconciliation windows for Payment rows stuck in status="pending" after an
# IntouchPay requestpayment call. Never trust the webhook alone — see
# app/api/webhooks.py and app/services/intouchpay.py for why.
PENDING_RECONCILE_AFTER_MINUTES = 15   # start checking stuck-pending rows after this long
PENDING_HARD_FAIL_AFTER_MINUTES = 24 * 60  # give up and mark failed after this long

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

async def _reconcile_pending_intouchpay_payments_async():
    db = SessionLocal()
    try:
        reconcile_cutoff = datetime.utcnow() - timedelta(minutes=PENDING_RECONCILE_AFTER_MINUTES)
        hard_fail_cutoff = datetime.utcnow() - timedelta(minutes=PENDING_HARD_FAIL_AFTER_MINUTES)

        stuck = db.query(Payment).filter(
            Payment.status == "pending",
            Payment.verified_at < reconcile_cutoff,
        ).all()

        if not stuck:
            logger.info("No stuck-pending IntouchPay payments found.")
            return

        for payment in stuck:
            try:
                status_resp = await get_transaction_status(payment.tx_ref)
                classification = classify_status(status_resp)
            except IntouchPayError as exc:
                logger.error(f"Reconciliation call failed for tx_ref={payment.tx_ref}: {exc}")
                classification = "unknown"

            if classification == "successful":
                payment.status = "successful"
                payment.verified_at = datetime.utcnow()
                logger.info(f"Reconciled stuck-pending payment tx_ref={payment.tx_ref} -> successful")
            elif classification == "unknown" and payment.verified_at < hard_fail_cutoff:
                # Never confirmed successful within a generous window — give up
                # rather than retry forever. Do NOT treat a bare "unknown" as
                # failure before this cutoff: IntouchPay's gettransactionstatus
                # response codes don't document a distinct decline code (see
                # app/services/intouchpay.py), so an "unknown" classification
                # could just mean "ask again later", not "it failed".
                payment.status = "failed"
                logger.info(f"Giving up on stuck-pending payment tx_ref={payment.tx_ref} after hard timeout -> failed")
            else:
                logger.info(f"Payment tx_ref={payment.tx_ref} still unresolved (classification={classification}); will retry next run")

        db.commit()
        logger.info(f"Reconciliation pass complete for {len(stuck)} stuck-pending payment(s).")
    except Exception as e:
        db.rollback()
        logger.error(f"Error while reconciling pending IntouchPay payments: {e}")
    finally:
        db.close()


def reconcile_pending_intouchpay_payments():
    """Check Payment rows stuck in status="pending" beyond a reasonable window
    via IntouchPay's authenticated get_transaction_status() API — the webhook
    (POST /api/webhooks/intouchpay) is the primary confirmation path, but it
    can be missed or never delivered, so this is the fallback reconciliation
    the IntouchPay docs themselves recommend under "Best Practices"."""
    if not settings.INTOUCH_USERNAME:
        logger.info("IntouchPay not configured (INTOUCH_USERNAME unset) — skipping reconciliation.")
        return
    asyncio.run(_reconcile_pending_intouchpay_payments_async())


if __name__ == "__main__":
    check_expired_subscriptions()
    reconcile_pending_intouchpay_payments()

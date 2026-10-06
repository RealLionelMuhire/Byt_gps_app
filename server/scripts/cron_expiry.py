import asyncio
import sys
import os
from datetime import datetime, timedelta

# Add the server directory to sys.path so we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.subscription import Subscription, Payment, SubscriptionPlan, SubscriptionReminder
from app.models.disbursement import Disbursement
from app.models.user import User
from app.services.expiry_reminders import due_stage, REMINDER_STAGES, EXPIRED_STAGE
from app.services.intouchpay import get_transaction_status, classify_status, IntouchPayError
from app.services.push_notifications import send_push_notification
from app.services.email import (
    send_subscription_expiring_email,
    send_subscription_expired_email,
    send_payment_failed_email,
)
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Reconciliation windows for Payment rows stuck in status="pending" after an
# IntouchPay requestpayment call. Never trust the webhook alone — see
# app/api/webhooks.py and app/services/intouchpay.py for why.
PENDING_RECONCILE_AFTER_MINUTES = 15   # start checking stuck-pending rows after this long
PENDING_HARD_FAIL_AFTER_MINUTES = 24 * 60  # give up and mark failed after this long

# The widest reminder window — a subscription further out than this can't be
# due for anything yet. The stages themselves live in
# app/services/expiry_reminders.py (shared with the admin dashboard).
EXPIRY_WARNING_DAYS = max(days for _, days in REMINDER_STAGES)


def _record_reminder(db, sub_id, clerk_user_id, stage, expires_at, email_sent, push_sent) -> None:
    """Log one sent reminder (migration 050) — what the admin dashboard
    reads to show who was reminded, when, and whether it actually went out."""
    db.add(SubscriptionReminder(
        subscription_id=sub_id,
        clerk_user_id=clerk_user_id,
        stage=stage,
        expires_at=expires_at,
        sent_at=datetime.utcnow(),
        email_sent=bool(email_sent),
        push_sent=bool(push_sent),
    ))

def _expiry_notification_copy(plan_id: str, plan_name: str) -> tuple:
    if plan_id == "trial":
        return (
            "⏳ Your free trial has ended",
            "Add a payment method to keep tracking your vehicles.",
        )
    return (
        "✅ Your subscription has completed",
        f"Your {plan_name} plan has completed its term. Renew to keep tracking your vehicles.",
    )


async def _notify_expired_users(expired_targets: list) -> None:
    """Send the expiry push + email for each (subscription_id,
    clerk_user_id, plan_slug, expires_at) tuple, and log it as the
    subscription's "expired" reminder. Runs after the DB commit so a
    slow/failing push never blocks or rolls back the subscription-expiry
    update itself."""
    if not expired_targets:
        return
    db = SessionLocal()
    try:
        plans_by_slug = {p.slug.lower(): p for p in db.query(SubscriptionPlan).all()}
        for sub_id, clerk_user_id, plan_id, expires_at in expired_targets:
            user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
            if not user:
                continue
            plan = plans_by_slug.get((plan_id or "").lower())
            plan_name = plan.name if plan else (plan_id or "").capitalize()
            title, body = _expiry_notification_copy(plan_id, plan_name)
            push_sent = await send_push_notification(
                user,
                title=title,
                body=body,
                data={"type": "subscription_expired", "screen": "plan_upgrade"},
            )
            email_sent = await send_subscription_expired_email(user, plan_name)
            already = db.query(SubscriptionReminder.id).filter(
                SubscriptionReminder.subscription_id == sub_id,
                SubscriptionReminder.stage == EXPIRED_STAGE,
                SubscriptionReminder.expires_at == expires_at,
            ).first()
            if not already:
                _record_reminder(db, sub_id, clerk_user_id, EXPIRED_STAGE, expires_at, email_sent, push_sent)
                db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error while notifying expired subscriptions: {e}")
    finally:
        db.close()


async def _notify_expiring_subscriptions_async() -> None:
    """Send the staged "expiring soon" reminders (14, 7 and 3 days before
    Subscription.expires_at — see app/services/expiry_reminders.py for
    exactly which stage is due when). Each (subscription, stage, expiry) is
    sent at most once, guarded by its subscription_reminders row, which also
    records whether the email and push were actually accepted. The row is
    committed per subscription as it's sent — a crash partway through still
    leaves already-sent ones correctly logged instead of re-sending them next
    run. expiry_reminder_sent_at (migration 036) is kept up to date as "most
    recent reminder" for older readers."""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        window_end = now + timedelta(days=EXPIRY_WARNING_DAYS)
        candidates = (
            db.query(Subscription)
            .options(joinedload(Subscription.plan))
            .filter(
                Subscription.status == "active",
                Subscription.is_recurring == False,
                Subscription.expires_at > now,
                Subscription.expires_at <= window_end,
            )
            .all()
        )

        if not candidates:
            logger.info("No subscriptions inside an expiry-warning window.")
            return

        for sub in candidates:
            stage = due_stage(sub.status, sub.started_at, sub.expires_at, now)
            if stage is None:
                continue
            already = db.query(SubscriptionReminder.id).filter(
                SubscriptionReminder.subscription_id == sub.id,
                SubscriptionReminder.stage == stage,
                SubscriptionReminder.expires_at == sub.expires_at,
            ).first()
            if already:
                continue
            user = db.query(User).filter(User.clerk_user_id == sub.clerk_user_id).first()
            if not user:
                continue
            # Subscription.plan_id is a real FK (migrations 041/042) —
            # sub.plan is the eager-loaded relationship, not a slug lookup.
            plan_name = sub.plan.name if sub.plan else "Unknown"
            days_left = max(0, (sub.expires_at - now).days)

            push_sent = await send_push_notification(
                user,
                title="⏳ Your plan is about to expire",
                body=f"Your {plan_name} plan expires in {days_left} day(s). Renew to keep tracking your vehicles.",
                data={"type": "subscription_expiring", "screen": "billing"},
            )
            email_sent = await send_subscription_expiring_email(user, sub, plan_name, days_left)

            sent_at = datetime.utcnow()
            _record_reminder(db, sub.id, sub.clerk_user_id, stage, sub.expires_at, email_sent, push_sent)
            sub.expiry_reminder_sent_at = sent_at
            db.commit()
            logger.info(
                "Sent %s expiry reminder for user %s (plan=%s, days_left=%d, email=%s, push=%s)",
                stage, sub.clerk_user_id, plan_name, days_left, bool(email_sent), bool(push_sent),
            )
    except Exception as e:
        db.rollback()
        logger.error(f"Error while notifying expiring subscriptions: {e}")
    finally:
        db.close()


def notify_expiring_subscriptions():
    """Entry point for __main__ — see _notify_expiring_subscriptions_async."""
    asyncio.run(_notify_expiring_subscriptions_async())


def check_expired_subscriptions():
    db = SessionLocal()
    try:
        now = datetime.utcnow()

        # Only one-time (non-recurring) subscriptions expire after their
        # duration. Recurring subscriptions auto-renew and are never flipped
        # to expired/completed by this cron — they stay active until the
        # user or admin explicitly cancels them.
        expired_subs = (
            db.query(Subscription)
            .options(joinedload(Subscription.plan))
            .filter(
                Subscription.status == "active",
                Subscription.is_recurring == False,
                Subscription.expires_at < now,
            )
            .all()
        )

        if not expired_subs:
            logger.info("No expired subscriptions found.")
            return

        notify_targets = []
        for sub in expired_subs:
            # notify_targets carries the plan *slug* (not Subscription.plan_id
            # itself, a real integer FK as of migrations 041/042) — both
            # _expiry_notification_copy's "trial" check and
            # _notify_expired_users' slug lookup below expect a slug string.
            plan_slug = sub.plan.slug if sub.plan else ""
            logger.info(
                f"Subscription for user {sub.clerk_user_id} (plan {plan_slug}) "
                f"reached end of term — marking completed"
            )
            # "completed" = the subscription ran its full duration and
            # naturally ended. "expired" is reserved for subscriptions that
            # lapsed without fulfilling their term (admin override, etc.).
            sub.status = "completed"
            sub.updated_at = datetime.utcnow()
            notify_targets.append((sub.id, sub.clerk_user_id, plan_slug, sub.expires_at))

        db.commit()
        logger.info(f"Successfully completed {len(expired_subs)} subscriptions.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error while checking expired subscriptions: {e}")
        return
    finally:
        db.close()

    # Notify after the DB commit — a slow/failing push must never block or
    # roll back the subscription-expiry update itself.
    asyncio.run(_notify_expired_users(notify_targets))

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

        hard_failed_payments = []
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
                hard_failed_payments.append(payment.clerk_user_id)
                logger.info(f"Giving up on stuck-pending payment tx_ref={payment.tx_ref} after hard timeout -> failed")
            else:
                logger.info(f"Payment tx_ref={payment.tx_ref} still unresolved (classification={classification}); will retry next run")

        db.commit()
        logger.info(f"Reconciliation pass complete for {len(stuck)} stuck-pending payment(s).")
    except Exception as e:
        db.rollback()
        logger.error(f"Error while reconciling pending IntouchPay payments: {e}")
        return
    finally:
        db.close()

    # Notify after the DB commit, same reasoning as check_expired_subscriptions.
    if hard_failed_payments:
        await _notify_hard_failed_payments(hard_failed_payments)


async def _notify_hard_failed_payments(clerk_user_ids: list) -> None:
    db = SessionLocal()
    try:
        for clerk_user_id in clerk_user_ids:
            user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
            if not user:
                continue
            payment = db.query(Payment).filter(
                Payment.clerk_user_id == clerk_user_id,
                Payment.status == "failed",
            ).order_by(Payment.verified_at.desc()).first()
            if payment:
                await send_payment_failed_email(user, payment)
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


async def _reconcile_pending_disbursements_async():
    """Mirror of _reconcile_pending_intouchpay_payments_async for
    Disbursement (B2C deposit) rows — see app/api/disbursements.py. Money
    already left the business on these, so a stuck-pending row here matters
    more than a stuck Payment: it's the difference between knowing a payout
    actually landed versus not. No "hard fail" notification email exists for
    disbursements yet (there's no recipient-facing email for this at all,
    unlike Payment's failure email) — add one if that's ever needed; for now
    this only updates status for admin visibility via GET /api/admin/disbursements.
    """
    db = SessionLocal()
    try:
        reconcile_cutoff = datetime.utcnow() - timedelta(minutes=PENDING_RECONCILE_AFTER_MINUTES)
        hard_fail_cutoff = datetime.utcnow() - timedelta(minutes=PENDING_HARD_FAIL_AFTER_MINUTES)

        stuck = db.query(Disbursement).filter(
            Disbursement.status == "pending",
            Disbursement.created_at < reconcile_cutoff,
        ).all()

        if not stuck:
            logger.info("No stuck-pending IntouchPay disbursements found.")
            return

        for disbursement in stuck:
            try:
                status_resp = await get_transaction_status(
                    disbursement.tx_ref, disbursement.provider_transaction_id
                )
                classification = classify_status(status_resp)
            except IntouchPayError as exc:
                logger.error(f"Reconciliation call failed for disbursement tx_ref={disbursement.tx_ref}: {exc}")
                classification = "unknown"

            if classification == "successful":
                disbursement.status = "successful"
                disbursement.verified_at = datetime.utcnow()
                logger.info(f"Reconciled stuck-pending disbursement tx_ref={disbursement.tx_ref} -> successful")
            elif classification == "unknown" and disbursement.created_at < hard_fail_cutoff:
                # Same "unknown is not failure" caution as payments — but
                # unlike a payment, a stuck disbursement staying "pending"
                # forever after this cutoff needs a human to actually check
                # IntouchPay/the recipient rather than silently retrying;
                # marking it "failed" here is a bookkeeping label prompting
                # that manual check, not a claim the funds definitely never moved.
                disbursement.status = "failed"
                logger.warning(
                    f"Giving up on stuck-pending disbursement tx_ref={disbursement.tx_ref} after hard timeout "
                    "-> failed (manual verification recommended before assuming funds never moved)"
                )
            else:
                logger.info(
                    f"Disbursement tx_ref={disbursement.tx_ref} still unresolved "
                    f"(classification={classification}); will retry next run"
                )

        db.commit()
        logger.info(f"Disbursement reconciliation pass complete for {len(stuck)} stuck-pending row(s).")
    except Exception as e:
        db.rollback()
        logger.error(f"Error while reconciling pending IntouchPay disbursements: {e}")
    finally:
        db.close()


def reconcile_pending_disbursements():
    """Check Disbursement rows stuck in status="pending" — same rationale as
    reconcile_pending_intouchpay_payments, for outbound B2C deposits instead
    of inbound C2B collections."""
    if not settings.INTOUCH_USERNAME:
        logger.info("IntouchPay not configured (INTOUCH_USERNAME unset) — skipping disbursement reconciliation.")
        return
    asyncio.run(_reconcile_pending_disbursements_async())


if __name__ == "__main__":
    check_expired_subscriptions()
    notify_expiring_subscriptions()
    reconcile_pending_intouchpay_payments()
    reconcile_pending_disbursements()

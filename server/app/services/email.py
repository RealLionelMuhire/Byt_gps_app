"""
Transactional email via EmailJS's REST API.

Only a server-to-server sender: this backend calls EmailJS's HTTP API
directly with the account's Private Key (accessToken), the same way
app/services/push_notifications.py calls FCM/Expo — never raises, only logs
and returns False on any failure, so a broken/unconfigured email integration
can never break a payment webhook, cron job, or any other caller's own flow.

Each event type has its own EmailJS template (EMAILJS_TEMPLATE_ID_* in
app/core/config.py) — the actual subject/body copy lives in that template in
the EmailJS dashboard; this module only supplies template_params. Keep those
params generic (to_email, to_name, subject, message, plus a few event-
specific fields) since the exact variable names a template uses are defined
by whoever authors the template, not by this code.
"""

import logging
from typing import Optional

import httpx

from app.core.config import settings
from app.models.user import User
from app.models.subscription import Payment, Subscription

logger = logging.getLogger(__name__)

_EMAILJS_SEND_URL = "https://api.emailjs.com/api/v1.0/email/send"


def _configured() -> bool:
    return bool(
        settings.EMAILJS_SERVICE_ID
        and settings.EMAILJS_PRIVATE_KEY
        and settings.EMAILJS_PUBLIC_KEY
    )


async def send_email(
    to_email: str,
    to_name: str,
    template_id: Optional[str],
    template_params: dict,
) -> bool:
    """
    Send one email via EmailJS. Returns True only if EmailJS accepted the
    request (HTTP 200). Never raises — logs and returns False for: EmailJS
    not configured, a missing/unset template_id for this event type, or any
    network/HTTP failure.
    """
    if not _configured():
        logger.info("EmailJS not configured (SERVICE_ID/PRIVATE_KEY/PUBLIC_KEY) — skipping email to %s", to_email)
        return False

    if not template_id:
        logger.info("No EmailJS template configured for this event — skipping email to %s", to_email)
        return False

    if not to_email:
        logger.warning("send_email called with no recipient address — skipping")
        return False

    payload = {
        "service_id": settings.EMAILJS_SERVICE_ID,
        "template_id": template_id,
        "user_id": settings.EMAILJS_PUBLIC_KEY,
        "accessToken": settings.EMAILJS_PRIVATE_KEY,
        "template_params": {
            "to_email": to_email,
            "to_name": to_name or to_email,
            **template_params,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _EMAILJS_SEND_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code == 200:
            logger.info("EmailJS email sent to %s (template=%s)", to_email, template_id)
            return True
        logger.warning(
            "EmailJS send failed for %s (template=%s): %d %s",
            to_email, template_id, resp.status_code, resp.text[:200],
        )
        return False
    except Exception as exc:
        logger.error("Failed to send EmailJS email to %s (template=%s): %s", to_email, template_id, exc)
        return False


def _user_display_name(user: User) -> str:
    name = f"{(user.first_name or '').strip()} {(user.last_name or '').strip()}".strip()
    return name or user.email


async def send_payment_receipt_email(user: User, payment: Payment, plan_name: str) -> bool:
    """Sent when a Payment row is confirmed successful (webhook or cron reconciliation)."""
    return await send_email(
        to_email=user.email,
        to_name=_user_display_name(user),
        template_id=settings.EMAILJS_TEMPLATE_ID_RECEIPT,
        template_params={
            "subject": "Payment received — Track IQ",
            "plan_name": plan_name,
            "amount": f"{payment.amount:.0f}",
            "currency": payment.currency,
            "tx_ref": payment.tx_ref,
            "message": f"We received your payment of {payment.amount:.0f} {payment.currency} for the {plan_name} plan.",
        },
    )


async def send_subscription_expiring_email(
    user: User, subscription: Subscription, plan_name: str, days_left: int
) -> bool:
    """Sent once, a few days before Subscription.expires_at, alongside the equivalent push."""
    return await send_email(
        to_email=user.email,
        to_name=_user_display_name(user),
        template_id=settings.EMAILJS_TEMPLATE_ID_EXPIRING,
        template_params={
            "subject": "Your Track IQ plan is about to expire",
            "plan_name": plan_name,
            "days_left": str(days_left),
            "expires_at": subscription.expires_at.isoformat(),
            "message": f"Your {plan_name} plan expires in {days_left} day(s). Renew to keep tracking your vehicles.",
        },
    )


async def send_subscription_expired_email(user: User, plan_name: str) -> bool:
    """Sent when a subscription has just been marked expired, alongside the equivalent push."""
    return await send_email(
        to_email=user.email,
        to_name=_user_display_name(user),
        template_id=settings.EMAILJS_TEMPLATE_ID_EXPIRED,
        template_params={
            "subject": "Your Track IQ plan has expired",
            "plan_name": plan_name,
            "message": f"Your {plan_name} plan has expired. Renew to keep tracking your vehicles.",
        },
    )


async def send_payment_failed_email(user: User, payment: Payment) -> bool:
    """Sent when a payment is confirmed failed (webhook) or given up on after the
    reconciliation cron's hard-fail timeout."""
    return await send_email(
        to_email=user.email,
        to_name=_user_display_name(user),
        template_id=settings.EMAILJS_TEMPLATE_ID_PAYMENT_FAILED,
        template_params={
            "subject": "Your Track IQ payment could not be completed",
            "amount": f"{payment.amount:.0f}",
            "currency": payment.currency,
            "tx_ref": payment.tx_ref,
            "message": f"Your payment of {payment.amount:.0f} {payment.currency} could not be completed. Please try again.",
        },
    )

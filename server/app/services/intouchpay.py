"""
IntouchPay (Rwanda mobile money) client.

Reference: IntouchPay HTTP API Reference Guide (intouchpay.co.rw/http-api),
captured 2026-08-28. All requests are JSON POSTs (not form-encoded) —
authenticated by a per-request SHA256 password hash. There are no API keys
or Authorization headers.

SECURITY NOTE — no callback signature: per the "Webhooks & Callbacks" section
of the reference guide, IntouchPay's callback carries no shared secret,
signature header, or documented IP allowlist. The only stated protection is
that the callback URL must be pre-registered ("whitelisted") with IntouchPay
before it will receive notifications. Because of this, the webhook handler
(app/api/webhooks.py: POST /api/webhooks/intouchpay) never trusts the
callback body's status directly — it uses the callback only as a trigger to
re-check the transaction via get_transaction_status() below, which is an
authenticated, server-initiated call carrying our own SHA256-hashed
credentials, and acts on that response instead.

DOC GAP: the gettransactionstatus response codes table only documents
"1000" (Pending) and "01" (Transaction Successful for Payment Transaction).
No responsecode is documented for a customer-declined or timed-out payment.
classify_status() below never returns a definitive failure — callers must
decide how to treat "unknown" (see webhooks.py and scripts/cron_expiry.py).
Confirm the actual failure responsecode against the sandbox before relying
on this endpoint to detect declines.
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Sandbox and production are different hosts, not just different paths.
_SANDBOX_REQUESTPAYMENT_URL = "https://developer.intouchpay.co.rw/api/v1/sandbox/requestpayment/"
_SANDBOX_STATUS_URL = "https://developer.intouchpay.co.rw/api/v1/sandbox/gettransactionstatus/"
_PROD_REQUESTPAYMENT_URL = "https://www.intouchpay.co.rw/api/requestpayment/"
_PROD_STATUS_URL = "https://www.intouchpay.co.rw/api/gettransactionstatus/"

# responsecode values this module acts on directly (see DOC GAP above).
RESPONSECODE_PENDING = "1000"
RESPONSECODE_PAYMENT_SUCCESSFUL = "01"  # "Transaction Successful for Payment Transaction"

_TIMEOUT = httpx.Timeout(15.0)


class IntouchPayError(Exception):
    """Raised when IntouchPay is unreachable or returns a non-2xx response."""


def _requestpayment_url() -> str:
    return _SANDBOX_REQUESTPAYMENT_URL if settings.INTOUCH_SANDBOX else _PROD_REQUESTPAYMENT_URL


def _status_url() -> str:
    return _SANDBOX_STATUS_URL if settings.INTOUCH_SANDBOX else _PROD_STATUS_URL


def _timestamp() -> str:
    """Current UTC timestamp in the exact format IntouchPay requires: YYYYMMDDHHmmss."""
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _password_hash(timestamp: str) -> str:
    """SHA256(username + accountno + partnerpassword + timestamp), lowercase hex."""
    raw = f"{settings.INTOUCH_USERNAME}{settings.INTOUCH_ACCOUNT_NO}{settings.INTOUCH_PARTNER_PASSWORD}{timestamp}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _auth_fields() -> dict:
    """The auth fields required on every call — freshly timestamped/hashed per call
    (the docs warn a stale timestamp may be rejected)."""
    ts = _timestamp()
    return {
        "username": settings.INTOUCH_USERNAME,
        "accountno": settings.INTOUCH_ACCOUNT_NO,
        "timestamp": ts,
        "password": _password_hash(ts),
    }


async def request_payment(amount: float, phone: str, transaction_id: str) -> dict:
    """
    Initiate a C2B mobile money collection — the customer approves on their phone.

    `transaction_id` is OUR reference (requesttransactionid). Per the docs it
    "must be globally unique across all your requests" — generate it with a
    UUID or timestamp-prefixed value, never reuse one.

    Returns the immediate acknowledgment dict, e.g.:
      {"status": "Pending", "success": true, "responsecode": "1000",
       "transactionid": "...", "requesttransactionid": "...", ...}
    This confirms the USSD prompt was dispatched — NOT that payment succeeded.
    The final result only ever arrives via the webhook callback (or a
    get_transaction_status() reconciliation call).

    Raises IntouchPayError only when IntouchPay is unreachable or returns a
    body that isn't JSON. A structured error (e.g. a bad password) comes back
    as a normal dict with success=False — IntouchPay uses non-2xx HTTP status
    codes (e.g. 401) for some of these even though the docs' examples only
    show 200 responses, so this never uses raise_for_status(): that would
    discard the diagnostic responsecode/message in the body.
    """
    payload = {
        **_auth_fields(),
        "amount": amount,
        "mobilephone": phone,
        "requesttransactionid": transaction_id,
        "callbackurl": settings.INTOUCH_CALLBACK_URL,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(_requestpayment_url(), json=payload)
    except httpx.RequestError as exc:
        logger.error("IntouchPay requestpayment unreachable for tx=%s: %s", transaction_id, exc)
        raise IntouchPayError(str(exc)) from exc

    try:
        data = resp.json()
    except ValueError as exc:
        logger.error(
            "IntouchPay requestpayment returned non-JSON (status=%s) for tx=%s: %s",
            resp.status_code, transaction_id, resp.text[:500],
        )
        raise IntouchPayError(f"Non-JSON response (HTTP {resp.status_code})") from exc

    logger.info(
        "IntouchPay requestpayment: tx=%s http=%s responsecode=%s status=%s",
        transaction_id, resp.status_code, data.get("responsecode"), data.get("status"),
    )
    return data


async def get_transaction_status(request_transaction_id: str, transaction_id: Optional[str] = None) -> dict:
    """
    Authenticated server-to-server status check — the only trustworthy source
    of truth for whether a payment succeeded (see module docstring: the
    webhook callback itself carries no verifiable signature).

    `transaction_id` (IntouchPay's own id, returned in the requestpayment
    response and in the callback) is included when known. Payment.tx_ref only
    stores OUR requesttransactionid, not IntouchPay's transactionid, so this
    is frequently called with transaction_id=None — confirm against the
    sandbox that requesttransactionid alone is sufficient for a lookup before
    relying on this in production.

    Raises IntouchPayError only when IntouchPay is unreachable or returns a
    body that isn't JSON — see the note in request_payment() about why
    raise_for_status() is deliberately not used here.
    """
    payload = {
        **_auth_fields(),
        "requesttransactionid": request_transaction_id,
        "transactionid": transaction_id or "",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(_status_url(), json=payload)
    except httpx.RequestError as exc:
        logger.error("IntouchPay gettransactionstatus unreachable for tx=%s: %s", request_transaction_id, exc)
        raise IntouchPayError(str(exc)) from exc

    try:
        data = resp.json()
    except ValueError as exc:
        logger.error(
            "IntouchPay gettransactionstatus returned non-JSON (status=%s) for tx=%s: %s",
            resp.status_code, request_transaction_id, resp.text[:500],
        )
        raise IntouchPayError(f"Non-JSON response (HTTP {resp.status_code})") from exc

    logger.info(
        "IntouchPay gettransactionstatus: tx=%s http=%s responsecode=%s status=%s",
        request_transaction_id, resp.status_code, data.get("responsecode"), data.get("status"),
    )
    return data


def classify_status(status_response: dict) -> str:
    """
    Classify a get_transaction_status() response as 'successful', 'pending',
    or 'unknown'.

    'unknown' covers query errors (e.g. responsecode 3000/3100/3200 — bad
    request or transaction not found) AND any response IntouchPay hasn't
    documented as a distinct decline/failure code (see the DOC GAP note in
    this module's docstring). Callers must never treat 'unknown' as a
    confirmed failure on its own — only as "not confirmed successful yet".
    """
    if status_response.get("success") and status_response.get("responsecode") == RESPONSECODE_PAYMENT_SUCCESSFUL:
        return "successful"
    if status_response.get("responsecode") == RESPONSECODE_PENDING:
        return "pending"
    return "unknown"

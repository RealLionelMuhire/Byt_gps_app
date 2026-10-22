"""
Signed links to the web app's standalone /pay page (Web_Traq_IQ app/pay),
sent in billing emails so a customer can pay without signing in.

The token is the only thing that identifies the account to
POST /api/payments/public/initiate — an email address alone is not
accepted, because anyone can type one: that let any visitor learn whether
an address has an account and send Mobile Money prompts against it.
Format: base64url("<clerk_user_id>|<expiry unix>") + "." + HMAC-SHA256
hex, keyed by SECRET_KEY with a purpose prefix so a token is never
interchangeable with app/dashboard.py's session signatures.
"""

import base64
import hashlib
import hmac
import time
from typing import Optional

from app.core.config import settings

PAY_LINK_TTL_SECONDS = 30 * 24 * 3600
_PURPOSE = b"pay-link:"


def _sign(payload: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), _PURPOSE + payload.encode(), hashlib.sha256).hexdigest()


def make_pay_token(clerk_user_id: str, now: Optional[float] = None) -> str:
    expires = int((now if now is not None else time.time()) + PAY_LINK_TTL_SECONDS)
    payload = base64.urlsafe_b64encode(f"{clerk_user_id}|{expires}".encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload)}"


def verify_pay_token(token: str, now: Optional[float] = None) -> Optional[str]:
    """The clerk_user_id the token was issued for, or None if it is
    malformed, tampered with or expired."""
    if not token or token.count(".") != 1:
        return None
    payload, sig = token.split(".")
    if not hmac.compare_digest(_sign(payload), sig):
        return None
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
        clerk_user_id, expires = raw.rsplit("|", 1)
        expires = int(expires)
    except (ValueError, UnicodeDecodeError):
        return None
    if not clerk_user_id or expires < (now if now is not None else time.time()):
        return None
    return clerk_user_id

"""Which expiry reminder a subscription is due for.

Shared by scripts/cron_expiry.py (which sends them) and the admin dashboard
(app/api/admin_dashboard.py, which shows who was reminded and who is due
but not yet notified), so both always agree on what "due" means.

Stages, in days before Subscription.expires_at: 14, 7, 3, then "expired"
once it has lapsed. Only the tightest stage the subscription is currently
inside is due — a subscription first seen with 2 days left gets the 3-day
notice, never a late 14- and 7-day notice as well. A stage other than the
final one is skipped entirely when the subscription didn't exist yet when
that stage's window opened (a 14-day trial never gets a "14 days left"
notice on the day it starts); the final 3-day notice is always sent.
"""

from datetime import datetime, timedelta
from typing import Optional

# (stage key, days before expiry), tightest first.
REMINDER_STAGES = (("3d", 3), ("7d", 7), ("14d", 14))
FINAL_STAGE = "3d"
EXPIRED_STAGE = "expired"


def due_stage(status: Optional[str], started_at: Optional[datetime],
              expires_at: Optional[datetime], now: datetime) -> Optional[str]:
    """The pre-expiry reminder stage an active, unexpired subscription is in
    right now, or None (not active, already expired, or not inside any
    window yet)."""
    if status != "active" or expires_at is None or expires_at <= now:
        return None
    for stage, days in REMINDER_STAGES:
        window_opens = expires_at - timedelta(days=days)
        if now < window_opens:
            continue
        if stage != FINAL_STAGE and started_at is not None and started_at >= window_opens:
            # Subscription started inside this window — this notice would
            # arrive on day one. Only the final stage ignores this.
            return None
        return stage
    return None

"""Resolves the plan/subscription shown for a device.

Device-level plan links (the old `Device.plan_id` FK, admin-settable via
PUT /api/devices/{id}/plan and POST /admin/devices/{imei}/plan) have been
retired. Vehicle-limit enforcement (`create_vehicle` in app/api/onboarding.py)
only ever reads the owner's account-wide `Subscription` — a device-level
override never affected what a user could actually do, only what the admin
billing screens displayed, and the two could silently disagree. `Device.plan_id`
is kept as a column for historical FK integrity only; nothing reads or writes
it anymore. Every place that used to show "this device's linked plan" now
resolves the owner's own Subscription instead, so the admin UI can never again
show something different from what enforcement actually uses.

`Subscription.plan_id` is a real FK to `subscription_plans.id` as of
migrations 041/042 (it used to be a bare slug string) — resolving a
subscription's plan is now a plain relationship traversal (`sub.plan`),
eager-loadable via `joinedload`, rather than a manual slug-keyed dict lookup.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from app.models.subscription import Subscription, SubscriptionPlan
from app.models.user import User


@dataclass
class ResolvedPlan:
    subscription: Optional[Subscription]
    plan: Optional[SubscriptionPlan]
    status: str  # active | expired | none


def resolve_owner_plan(db: Session, owner: Optional[User]) -> ResolvedPlan:
    """The plan/subscription that actually applies to `owner` right now (or
    most recently, if they've never had one active) — the single source of
    truth every device belonging to this owner should display."""
    if owner is None or not owner.clerk_user_id:
        return ResolvedPlan(None, None, "none")

    sub = (
        db.query(Subscription)
        .options(joinedload(Subscription.plan))
        .filter(Subscription.clerk_user_id == owner.clerk_user_id)
        .order_by(Subscription.created_at.desc())
        .first()
    )
    if sub is None:
        return ResolvedPlan(None, None, "none")

    now = datetime.utcnow()
    active = sub.status == "active" and sub.expires_at and sub.expires_at > now
    return ResolvedPlan(sub, sub.plan, "active" if active else "expired")

"""
Per-vehicle subscription pricing and coverage (migration 048).

Every plan is priced per vehicle. A subscription has one start/renewal date
and a number of paid vehicle slots (Subscription.quantity); the vehicles it
covers are SubscriptionVehicle rows, never more than `quantity`. Adding a
vehicle takes a free slot at no charge if there is one; otherwise the extra
slots are charged prorated for the time left until the shared renewal date.

Used by app/api/onboarding.py's payment/quote/activation endpoints so the
amount quoted, the amount charged, and what activation covers can't drift.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from sqlalchemy.orm import Session

from app.models.subscription import Payment, Subscription, SubscriptionVehicle
from app.models.vehicle import Vehicle


class SelectionError(ValueError):
    """A vehicle selection that can't be priced/activated; the message is
    written for the customer and returned as a 400."""


def owner_vehicles(db: Session, clerk_user_id: str) -> List[Vehicle]:
    return (
        db.query(Vehicle)
        .filter(Vehicle.clerk_user_id == clerk_user_id)
        .order_by(Vehicle.id.asc())
        .all()
    )


def resolve_selection(db: Session, clerk_user_id: str, vehicle_ids: Optional[Iterable[int]]) -> List[Vehicle]:
    """The customer's chosen vehicles, validated as theirs. None means "all
    of them" — what app versions from before vehicle selection get, which
    matches how they were charged (per paired vehicle)."""
    owned = owner_vehicles(db, clerk_user_id)
    if vehicle_ids is None:
        return owned
    ids = list(dict.fromkeys(vehicle_ids))
    if not ids:
        raise SelectionError("Choose at least one vehicle.")
    by_id = {v.id: v for v in owned}
    if any(i not in by_id for i in ids):
        raise SelectionError("One or more of the chosen vehicles aren't on your account.")
    return [by_id[i] for i in ids]


def check_slot_cap(cfg: dict, plan_name: str, vehicle_count: int) -> None:
    cap = cfg.get("max_devices")
    if cap and vehicle_count > cap:
        raise SelectionError(
            f"{plan_name} covers up to {cap} vehicle{'' if cap == 1 else 's'}. "
            "Choose fewer vehicles or another plan."
        )


def monthly_price(price: float, duration_days: int) -> float:
    """Per-vehicle price normalized to 30 days, for comparing plans of
    different lengths."""
    return round(float(price) * 30 / max(1, duration_days), 2)


def subscribe_amount(cfg: dict, vehicle_count: int) -> float:
    """What a new subscription costs: the per-vehicle price times the
    vehicles chosen (at least one — a subscription always has a slot)."""
    return round(float(cfg.get("price", 0.0)) * max(1, vehicle_count), 2)


def covered_vehicle_ids(db: Session, subscription: Subscription) -> set:
    return {
        vid for (vid,) in db.query(SubscriptionVehicle.vehicle_id)
        .filter(SubscriptionVehicle.subscription_id == subscription.id)
        .all()
    }


@dataclass
class AddVehiclesPlan:
    new_vehicles: List[Vehicle]
    free_slots_used: int
    extra_slots: int
    unit_price: float        # prorated price per extra slot
    amount: float
    remaining_days: int
    period_days: int


def plan_add_vehicles(
    db: Session, subscription: Subscription, cfg: dict, plan_name: str,
    vehicles: List[Vehicle], now: Optional[datetime] = None,
) -> AddVehiclesPlan:
    """Price adding `vehicles` to an active subscription: free slots first,
    then extra slots at the per-vehicle price prorated to the time left
    (rounded UP to a whole currency unit, so it never undercharges)."""
    now = now or datetime.utcnow()
    covered = covered_vehicle_ids(db, subscription)
    already = [v for v in vehicles if v.id in covered]
    if already:
        names = ", ".join(v.nickname or v.plate for v in already)
        raise SelectionError(f"Already on your plan: {names}.")
    check_slot_cap(cfg, plan_name, len(covered) + len(vehicles))

    free = max(0, (subscription.quantity or 0) - len(covered))
    extra = max(0, len(vehicles) - free)
    period = (subscription.expires_at - subscription.started_at).total_seconds()
    remaining = (subscription.expires_at - now).total_seconds()
    if remaining <= 0 and subscription.is_recurring and period > 0:
        # A recurring subscription is never expired and its expires_at is
        # never advanced, so once that first-period date is in the past
        # there is no time left to prorate against. Price a full new period
        # instead of 0 — 0 would quote "free" and make the add-vehicles flow
        # unreachable (quote 400s "no payment needed" while activation 402s
        # "payment required").
        remaining = period
    if period <= 0 or remaining <= 0:
        unit = 0.0
    else:
        unit = float(math.ceil(float(cfg.get("price", 0.0)) * remaining / period))
    return AddVehiclesPlan(
        new_vehicles=vehicles,
        free_slots_used=min(free, len(vehicles)),
        extra_slots=extra,
        unit_price=unit,
        amount=round(unit * extra, 2),
        remaining_days=max(0, math.ceil(remaining / 86400)),
        period_days=max(1, round(period / 86400)),
    )


def cover_vehicles(
    db: Session, subscription: Subscription, vehicles: Iterable[Vehicle], payment: Optional[Payment] = None,
) -> None:
    """Add coverage rows (skipping any already present). Caller commits."""
    covered = covered_vehicle_ids(db, subscription)
    for v in vehicles:
        if v.id not in covered:
            db.add(SubscriptionVehicle(
                subscription_id=subscription.id, vehicle_id=v.id,
                payment_id=payment.id if payment is not None else None,
            ))
            covered.add(v.id)


def vehicles_for_payment(db: Session, payment: Payment) -> List[Vehicle]:
    """The vehicles a payment was made for, still on the payer's account.
    A payment from before vehicle selection (vehicle_ids NULL) covers all of
    them, as it was charged."""
    owned = owner_vehicles(db, payment.clerk_user_id)
    if payment.vehicle_ids is None:
        return owned
    wanted = set(payment.vehicle_ids)
    return [v for v in owned if v.id in wanted]


def paid_slots(payment: Payment, vehicle_count: int) -> int:
    """Slots a subscribe-payment bought: the vehicles it was made for (a
    legacy payment: the vehicles it covers, at least one)."""
    if payment.vehicle_ids is not None:
        return max(1, len(payment.vehicle_ids))
    return max(1, vehicle_count)


def claim_payment(db: Session, payment: Payment, subscription: Subscription) -> bool:
    """Atomically mark `payment` as consumed by `subscription` (migration 046)."""
    claimed = (
        db.query(Payment)
        .filter(Payment.id == payment.id, Payment.consumed_at.is_(None))
        .update(
            {Payment.consumed_at: datetime.utcnow(), Payment.subscription_id: subscription.id},
            synchronize_session=False,
        )
    )
    return claimed == 1


def activate_payment_subscription(db: Session, payment: Payment) -> Optional[Subscription]:
    """Auto-activate an active subscription for a successful unconsumed payment."""
    if payment.status != "successful" or payment.consumed_at is not None:
        return None
    plan = payment.plan
    if plan is None:
        return None
    from app.api.subscriptions import plan_config
    cfg = plan_config(db, plan.slug)
    vehicles = vehicles_for_payment(db, payment)
    quantity = paid_slots(payment, len(vehicles))
    days = cfg.get("days", 30)
    expires_at = datetime.utcnow() + timedelta(days=days)

    sub = Subscription(
        clerk_user_id=payment.clerk_user_id,
        plan_id=plan.id,
        status="active",
        price=payment.amount,
        started_at=datetime.utcnow(),
        expires_at=expires_at,
        quantity=quantity,
        is_recurring=False,
    )
    db.add(sub)
    db.flush()
    if not claim_payment(db, payment, sub):
        db.rollback()
        return None
    cover_vehicles(db, sub, vehicles, payment)
    from app.models.user import User
    user = db.query(User).filter(User.clerk_user_id == payment.clerk_user_id).first()
    if user:
        user.onboarding_step = 9
        user.onboarding_complete = True
        user.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(sub)
    return sub


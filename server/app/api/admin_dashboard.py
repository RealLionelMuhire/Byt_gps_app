"""
Admin operations & sales dashboard API — every route requires require_admin
(SUPER_ADMIN / ADMIN). Backs car-management-portal's Operations page.

    GET /api/admin/dashboard/overview          KPI cards
    GET /api/admin/dashboard/revenue           revenue series (chart)
    GET /api/admin/dashboard/payments          payments table (+ ?format=csv)
    GET /api/admin/dashboard/attention         payments/payouts needing a human
    GET /api/admin/dashboard/clients           per-client table (+ ?format=csv)
    GET /api/admin/dashboard/clients/{user_id} one client in full
    GET /api/admin/dashboard/inventory         GPS devices (+ ?format=csv)
    GET /api/admin/dashboard/usage             GPS usage per device/client (+ ?format=csv)

Truthfulness rules every number here follows:

- Money is only ever read from `payments` / `disbursements` rows, never
  from Subscription.price or a plan's list price. Revenue = successful
  RWF payments; net = revenue minus successful refunds (disbursements with
  a reference_payment_id). Payments are dated by created_at (when the
  customer paid, migration 049) — verified_at moves when the webhook or
  the reconciliation cron resolves the payment, up to 24h later.
- A client's plan is their most recent Subscription (the same row
  app/services/plan_resolution.py resolves and enforcement reads). Its
  status is computed from status + expires_at at request time — never the
  bare status column, which scripts/cron_expiry.py only reconciles every
  15 minutes. The deprecated Device.plan_id is never read.
- Device connectivity comes from last_update (the last packet actually
  received), not Device.status, which is a TCP-session flag that stays
  "online" if the server dies without closing the socket.
- Dates are grouped by Africa/Kigali calendar day (UTC+2, no DST). Every
  timestamp in JSON is UTC with a "Z"; CSV exports are in Kigali time.

Aggregations run in SQL. Per-client rows are then filtered/sorted in Python
because the reminder stage (app/services/expiry_reminders.py, shared with
the cron) is a rule, not a column — that's one already-aggregated row per
client, never a full table.
"""

import csv
import io
import math
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, case, func, literal_column, or_, select
from sqlalchemy.orm import Session, joinedload

from app.core.auth import require_admin
from app.core.database import get_db
from app.core.serialization import _to_utc_iso
from app.models.device import Device
from app.models.disbursement import Disbursement
from app.models.location import Location
from app.models.subscription import (
    Payment, Subscription, SubscriptionPlan, SubscriptionReminder, SubscriptionVehicle,
)
from app.models.trip import Trip
from app.models.user import Role, User
from app.models.vehicle import Vehicle
from app.services.expiry_reminders import EXPIRED_STAGE, due_stage

router = APIRouter()

TIMEZONE = "Africa/Kigali"
KIGALI_OFFSET = timedelta(hours=2)  # UTC+2 all year, no DST
# A device counts as online if a packet arrived this recently. Both
# supported trackers heartbeat every few minutes while powered.
ONLINE_WINDOW = timedelta(minutes=10)
# Same threshold scripts/cron_expiry.py starts reconciling pending payments at.
STUCK_PENDING_AFTER = timedelta(minutes=15)
# A successful payment should activate its plan within seconds (the app
# calls POST /api/subscriptions right after IntouchPay confirms).
UNACTIVATED_AFTER = timedelta(minutes=30)
FAILED_LOOKBACK = timedelta(days=30)
CURRENCY = "RWF"
EXPIRY_WINDOWS = (3, 7, 14)
MAX_PAGE_SIZE = 500


# ── Small helpers ────────────────────────────────────────────────────────────

def _dt(value: Any) -> Optional[datetime]:
    """Aggregates (MAX over a CASE) can come back as strings on SQLite."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return _to_utc_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _kigali_text(value: Optional[datetime]) -> str:
    return (value + KIGALI_OFFSET).strftime("%Y-%m-%d %H:%M") if value else ""


def _kigali_today(now: datetime) -> date:
    return (now + KIGALI_OFFSET).date()


def _utc_start_of(day: date) -> datetime:
    """UTC instant of 00:00 Kigali time on `day`."""
    return datetime(day.year, day.month, day.day) - KIGALI_OFFSET


def _date_range(date_from: Optional[date], date_to: Optional[date], now: datetime,
                default_days: int = 30) -> tuple[date, date, datetime, datetime]:
    """Inclusive Kigali-calendar range -> [start, end) UTC bounds."""
    to_day = date_to or _kigali_today(now)
    from_day = date_from or (to_day - timedelta(days=default_days - 1))
    if from_day > to_day:
        raise HTTPException(status_code=422, detail="'from' must be on or before 'to'")
    return from_day, to_day, _utc_start_of(from_day), _utc_start_of(to_day + timedelta(days=1))


def _effective_status(sub: Optional[Subscription], now: datetime) -> str:
    if sub is None:
        return "none"
    if sub.status == "cancelled":
        return "cancelled"
    if sub.status == "active":
        # Recurring subscriptions never lapse: expires_at is never advanced
        # past the first period and cron_expiry.py never flips them, so the
        # date alone must not be read as an expiry (same rule as
        # plan_resolution, devices.py and dashboard.py).
        if sub.is_recurring:
            return "active"
        if sub.expires_at and sub.expires_at > now:
            return "active"
    return "expired"


def _days_remaining(expires_at: Optional[datetime], now: datetime) -> Optional[float]:
    if expires_at is None:
        return None
    return round((expires_at - now).total_seconds() / 86400, 2)


def _expiry_bucket(status: str, days: Optional[float]) -> Optional[str]:
    if status == "expired":
        return "expired"
    if status != "active" or days is None:
        return None
    for window in EXPIRY_WINDOWS:
        if days <= window:
            return f"le{window}"
    return "later"


def _connectivity(last_update: Optional[datetime], now: datetime) -> str:
    if last_update is None:
        return "never"
    return "online" if last_update >= now - ONLINE_WINDOW else "offline"


def _display_name(user: Optional[User]) -> Optional[str]:
    if user is None:
        return None
    return f"{user.first_name or ''} {user.last_name or ''}".strip() or user.email


def _sorted(items: list[dict], key: str, desc: bool) -> list[dict]:
    """Sort with missing values always last, whichever direction."""
    present = [i for i in items if i.get(key) is not None]
    missing = [i for i in items if i.get(key) is None]
    present.sort(key=lambda i: i[key].lower() if isinstance(i[key], str) else i[key], reverse=desc)
    return present + missing


def _page(items: list[dict], page: int, page_size: int) -> dict:
    start = (page - 1) * page_size
    return {
        "items": _jsonable(items[start:start + page_size]),
        "total": len(items),
        "page": page,
        "page_size": page_size,
    }


def _matches(q: Optional[str], *fields: Optional[str]) -> bool:
    if not q:
        return True
    needle = q.strip().lower()
    return any(needle in (f or "").lower() for f in fields)


def _csv_response(filename: str, header: list[str], rows: Iterable[list]) -> StreamingResponse:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _bucket_expr(db: Session, column, granularity: str):
    """SQL expression: the Kigali calendar day/week(Monday)/month a UTC
    timestamp falls in, as 'YYYY-MM-DD' text."""
    if db.get_bind().dialect.name == "postgresql":
        shifted = column + literal_column("interval '2 hours'")
        return func.to_char(func.date_trunc(granularity, shifted), "YYYY-MM-DD")
    # SQLite (the test suite).
    if granularity == "day":
        return func.date(column, "+2 hours")
    if granularity == "week":
        return func.date(column, "+2 hours", "-6 days", "weekday 1")
    return func.strftime("%Y-%m-01", column, "+2 hours")


def _bucket_keys(from_day: date, to_day: date, granularity: str) -> list[str]:
    if granularity == "day":
        start, step = from_day, None
    elif granularity == "week":
        start, step = from_day - timedelta(days=from_day.weekday()), None
    else:
        start, step = from_day.replace(day=1), "month"
    keys, cur = [], start
    while cur <= to_day:
        keys.append(cur.isoformat())
        if step == "month":
            cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            cur += timedelta(days=7 if granularity == "week" else 1)
    return keys


# ── Shared aggregate builders ────────────────────────────────────────────────

def _latest_subscriptions(db: Session, clerk_ids: Optional[list[str]] = None) -> dict[str, Subscription]:
    """Each client's most recent Subscription (any status) — the same row
    resolve_owner_plan() returns — in two queries."""
    rn = func.row_number().over(
        partition_by=Subscription.clerk_user_id,
        order_by=(Subscription.created_at.desc(), Subscription.id.desc()),
    ).label("rn")
    inner = select(Subscription.id.label("id"), rn)
    if clerk_ids is not None:
        inner = inner.where(Subscription.clerk_user_id.in_(clerk_ids))
    inner = inner.subquery()
    ids = [row.id for row in db.execute(select(inner.c.id).where(inner.c.rn == 1))]
    if not ids:
        return {}
    subs = (
        db.query(Subscription).options(joinedload(Subscription.plan))
        .filter(Subscription.id.in_(ids)).all()
    )
    return {s.clerk_user_id: s for s in subs}


def _refunded_by_payment(db: Session, payment_ids: list[int]) -> dict[int, float]:
    if not payment_ids:
        return {}
    rows = (
        db.query(Disbursement.reference_payment_id, func.sum(Disbursement.amount))
        .filter(Disbursement.reference_payment_id.in_(payment_ids),
                Disbursement.status == "successful")
        .group_by(Disbursement.reference_payment_id).all()
    )
    return {pid: float(total or 0) for pid, total in rows}


def _reminder_summary(sub: Optional[Subscription], reminders: list[SubscriptionReminder],
                      now: datetime) -> dict:
    """Reminders sent for the subscription's CURRENT expiry date, and which
    one (if any) is due right now but hasn't gone out yet."""
    if sub is None:
        return {"reminders_sent": [], "last_reminder": None, "reminder_due": None}
    current = sorted(
        (r for r in reminders if r.expires_at == sub.expires_at),
        key=lambda r: r.sent_at,
    )
    sent_stages = {r.stage for r in current}
    due = due_stage(sub.status, sub.started_at, sub.expires_at, now)
    if due is None and sub.status == "active" and not sub.is_recurring and sub.expires_at <= now:
        # Lapsed but the 15-minute cron hasn't marked it (or sent the
        # expiry notice) yet. Recurring subs are excluded: they are never
        # completed by the cron, so an "expiry due" flag would stick
        # forever on a subscription that is meant to keep running.
        due = EXPIRED_STAGE
    last = current[-1] if current else None
    return {
        "reminders_sent": [
            {"stage": r.stage, "sent_at": r.sent_at, "email_sent": r.email_sent, "push_sent": r.push_sent}
            for r in current
        ],
        "last_reminder": {
            "stage": last.stage, "sent_at": last.sent_at,
            "email_sent": last.email_sent, "push_sent": last.push_sent,
        } if last else None,
        "reminder_due": due if due and due not in sent_stages else None,
    }


def _client_rows(db: Session, now: datetime, only_user_id: Optional[int] = None) -> list[dict]:
    """One aggregated row per client: role USER, plus any other account that
    owns billing data or a device (so nothing paid for is hidden)."""
    users_q = db.query(User)
    if only_user_id is not None:
        users_q = users_q.filter(User.id == only_user_id)
    else:
        users_q = users_q.filter(or_(
            User.role == Role.USER,
            User.clerk_user_id.in_(select(Payment.clerk_user_id)),
            User.clerk_user_id.in_(select(Subscription.clerk_user_id)),
            User.id.in_(select(Device.user_id).where(Device.user_id.isnot(None))),
        ))
    users = users_q.all()
    if not users:
        return []
    clerk_ids = [u.clerk_user_id for u in users]
    user_ids = [u.id for u in users]

    successful = and_(Payment.status == "successful", Payment.currency == CURRENCY)
    pay = {
        r[0]: r for r in db.query(
            Payment.clerk_user_id,
            func.coalesce(func.sum(case((successful, Payment.amount), else_=0)), 0),
            func.coalesce(func.sum(case((Payment.status == "successful", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Payment.status == "pending", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Payment.status == "failed", 1), else_=0)), 0),
            func.max(case((Payment.status == "successful", Payment.created_at))),
        ).filter(Payment.clerk_user_id.in_(clerk_ids)).group_by(Payment.clerk_user_id).all()
    }
    refunds = dict(
        db.query(Disbursement.clerk_user_id, func.sum(Disbursement.amount))
        .filter(Disbursement.clerk_user_id.in_(clerk_ids), Disbursement.status == "successful",
                Disbursement.reference_payment_id.isnot(None), Disbursement.currency == CURRENCY)
        .group_by(Disbursement.clerk_user_id).all()
    )
    vehicles = dict(
        db.query(Vehicle.clerk_user_id, func.count(Vehicle.id))
        .filter(Vehicle.clerk_user_id.in_(clerk_ids)).group_by(Vehicle.clerk_user_id).all()
    )
    devices = {
        r[0]: r for r in db.query(
            Device.user_id,
            func.count(Device.id),
            func.coalesce(func.sum(case((Device.last_update >= now - ONLINE_WINDOW, 1), else_=0)), 0),
        ).filter(Device.user_id.in_(user_ids)).group_by(Device.user_id).all()
    }
    subs = _latest_subscriptions(db, clerk_ids)
    sub_ids = [s.id for s in subs.values()]
    covered = dict(
        db.query(SubscriptionVehicle.subscription_id, func.count(func.distinct(SubscriptionVehicle.vehicle_id)))
        .join(Vehicle, Vehicle.id == SubscriptionVehicle.vehicle_id)
        .join(Subscription, Subscription.id == SubscriptionVehicle.subscription_id)
        .filter(SubscriptionVehicle.subscription_id.in_(sub_ids),
                Vehicle.clerk_user_id == Subscription.clerk_user_id)
        .group_by(SubscriptionVehicle.subscription_id).all()
    ) if sub_ids else {}
    reminders_by_sub: dict[int, list[SubscriptionReminder]] = {}
    if sub_ids:
        for r in db.query(SubscriptionReminder).filter(SubscriptionReminder.subscription_id.in_(sub_ids)):
            reminders_by_sub.setdefault(r.subscription_id, []).append(r)

    rows = []
    for u in users:
        sub = subs.get(u.clerk_user_id)
        status = _effective_status(sub, now)
        # No countdown for recurring subs: their expires_at is a stale
        # first-period date (never advanced), which would render as
        # negative days and land them in the "expiring" buckets.
        days = (_days_remaining(sub.expires_at, now)
                if sub and status in ("active", "expired") and not sub.is_recurring
                else None)
        p = pay.get(u.clerk_user_id)
        gross = float(p[1]) if p else 0.0
        refunded = float(refunds.get(u.clerk_user_id) or 0)
        d = devices.get(u.id)
        rows.append({
            "user_id": u.id,
            "clerk_user_id": u.clerk_user_id,
            "name": _display_name(u),
            "email": u.email,
            "phone_number": u.phone_number,
            "role": u.role.value if hasattr(u.role, "value") else u.role,
            "created_at": u.created_at,
            "vehicles_total": int(vehicles.get(u.clerk_user_id) or 0),
            "vehicles_covered": int(covered.get(sub.id, 0)) if sub and status == "active" else 0,
            "devices_total": int(d[1]) if d else 0,
            "devices_online": int(d[2]) if d else 0,
            "subscription_id": sub.id if sub else None,
            "plan_slug": sub.plan.slug if sub and sub.plan else None,
            "plan_name": sub.plan.name if sub and sub.plan else None,
            "plan_price": sub.price if sub else None,
            "quantity": sub.quantity if sub else None,
            "status": status,
            "started_at": sub.started_at if sub else None,
            "expires_at": sub.expires_at if sub else None,
            "days_remaining": days,
            "expiry_bucket": _expiry_bucket(status, days),
            "total_paid": gross,
            "total_refunded": refunded,
            "net_paid": gross - refunded,
            "successful_payments": int(p[2]) if p else 0,
            "pending_payments": int(p[3]) if p else 0,
            "failed_payments": int(p[4]) if p else 0,
            "last_payment_at": _dt(p[5]) if p else None,
            **_reminder_summary(sub, reminders_by_sub.get(sub.id, []) if sub else [], now),
        })
    return rows


def _attention_items(db: Session, now: datetime) -> list[dict]:
    items = []
    users = {}

    def owner(clerk_id):
        if clerk_id not in users:
            users[clerk_id] = db.query(User).filter(User.clerk_user_id == clerk_id).first()
        return users[clerk_id]

    stuck = db.query(Payment).options(joinedload(Payment.plan)).filter(
        Payment.status == "pending", Payment.created_at < now - STUCK_PENDING_AFTER)
    failed = db.query(Payment).options(joinedload(Payment.plan)).filter(
        Payment.status == "failed", Payment.created_at >= now - FAILED_LOOKBACK)
    unactivated = db.query(Payment).options(joinedload(Payment.plan)).filter(
        Payment.status == "successful", Payment.consumed_at.is_(None),
        Payment.created_at < now - UNACTIVATED_AFTER)
    for reason, query, detail in (
        ("stuck_pending", stuck, "Pending for over 15 minutes — IntouchPay never confirmed. The cron keeps checking and marks it failed after 24h."),
        ("failed", failed, "Payment failed in the last 30 days — the client may need help paying."),
        ("paid_not_activated", unactivated, "Money received but no plan was activated with it — activate or refund."),
    ):
        for p in query.all():
            u = owner(p.clerk_user_id)
            items.append({
                "kind": "payment", "reason": reason, "detail": detail,
                "id": p.id, "tx_ref": p.tx_ref, "amount": p.amount, "currency": p.currency,
                "status": p.status, "created_at": p.created_at, "verified_at": p.verified_at,
                "plan_name": p.plan.name if p.plan else None,
                "age_minutes": int((now - p.created_at).total_seconds() // 60) if p.created_at else None,
                "user_id": u.id if u else None, "client_name": _display_name(u),
                "client_email": u.email if u else None, "clerk_user_id": p.clerk_user_id,
            })
    payouts = db.query(Disbursement).filter(or_(
        and_(Disbursement.status == "pending", Disbursement.created_at < now - STUCK_PENDING_AFTER),
        and_(Disbursement.status == "failed", Disbursement.created_at >= now - FAILED_LOOKBACK),
    ))
    for d in payouts.all():
        u = owner(d.clerk_user_id)
        items.append({
            "kind": "disbursement",
            "reason": "payout_pending" if d.status == "pending" else "payout_failed",
            "detail": ("Payout still unconfirmed after 15 minutes." if d.status == "pending" else
                       "Payout marked failed — verify with IntouchPay before assuming the money never moved."),
            "id": d.id, "tx_ref": d.tx_ref, "amount": d.amount, "currency": d.currency,
            "status": d.status, "created_at": d.created_at, "verified_at": d.verified_at,
            "plan_name": None,
            "age_minutes": int((now - d.created_at).total_seconds() // 60) if d.created_at else None,
            "user_id": u.id if u else None, "client_name": _display_name(u),
            "client_email": u.email if u else None, "clerk_user_id": d.clerk_user_id,
        })
    items.sort(key=lambda i: i["created_at"] or now, reverse=True)
    return items


# ── Overview ─────────────────────────────────────────────────────────────────

@router.get("/overview")
async def overview(
    date_from: Optional[date] = Query(None, alias="from"),
    date_to: Optional[date] = Query(None, alias="to"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    now = datetime.utcnow()
    from_day, to_day, start, end = _date_range(date_from, date_to, now)
    in_range = and_(Payment.created_at >= start, Payment.created_at < end)

    pay = db.query(
        func.coalesce(func.sum(case((and_(Payment.status == "successful", Payment.currency == CURRENCY), Payment.amount), else_=0)), 0),
        func.coalesce(func.sum(case((Payment.status == "successful", 1), else_=0)), 0),
        func.coalesce(func.sum(case((Payment.status == "pending", 1), else_=0)), 0),
        func.coalesce(func.sum(case((Payment.status == "failed", 1), else_=0)), 0),
        func.coalesce(func.sum(case((Payment.currency != CURRENCY, 1), else_=0)), 0),
    ).filter(in_range).one()
    disb = db.query(
        func.coalesce(func.sum(case((Disbursement.reference_payment_id.isnot(None), Disbursement.amount), else_=0)), 0),
        func.coalesce(func.sum(case((Disbursement.reference_payment_id.is_(None), Disbursement.amount), else_=0)), 0),
    ).filter(Disbursement.status == "successful", Disbursement.currency == CURRENCY,
             Disbursement.created_at >= start, Disbursement.created_at < end).one()
    gross, refunds, other_payouts = float(pay[0]), float(disb[0]), float(disb[1])

    attention = _attention_items(db, now)
    clients = _client_rows(db, now)
    subs = {"active": 0, "expired": 0, "cancelled": 0, "none": 0}
    for c in clients:
        subs[c["status"]] += 1
    expiring = {
        f"expiring_{w}": sum(1 for c in clients if c["status"] == "active"
                             and c["days_remaining"] is not None and c["days_remaining"] <= w)
        for w in EXPIRY_WINDOWS
    }

    inv = db.query(
        func.count(Device.id),
        func.coalesce(func.sum(case((Device.lifecycle == "registered", 1), else_=0)), 0),
        func.coalesce(func.sum(case((Device.lifecycle == "in_stock", 1), else_=0)), 0),
        func.coalesce(func.sum(case((Device.lifecycle == "sold", 1), else_=0)), 0),
        func.coalesce(func.sum(case((Device.last_update >= now - ONLINE_WINDOW, 1), else_=0)), 0),
        func.coalesce(func.sum(case((Device.last_update.is_(None), 1), else_=0)), 0),
    ).one()

    points = db.query(func.count(Location.id), func.count(func.distinct(Location.device_id))).filter(
        Location.timestamp >= start, Location.timestamp < end).one()
    trips = db.query(func.count(Trip.id), func.coalesce(func.sum(Trip.total_distance_km), 0)).filter(
        Trip.start_time >= start, Trip.start_time < end).one()

    return _jsonable({
        "generated_at": now,
        "range": {"from": from_day, "to": to_day, "timezone": TIMEZONE},
        "revenue": {
            "currency": CURRENCY,
            "gross": gross,
            "refunds": refunds,
            "net": gross - refunds,
            "other_payouts": other_payouts,
            "successful_count": int(pay[1]),
            "pending_count": int(pay[2]),
            "failed_count": int(pay[3]),
            "non_rwf_count": int(pay[4]),
        },
        "attention": {
            "stuck_pending": sum(1 for a in attention if a["reason"] == "stuck_pending"),
            "failed_recent": sum(1 for a in attention if a["reason"] == "failed"),
            "paid_not_activated": sum(1 for a in attention if a["reason"] == "paid_not_activated"),
            "payouts": sum(1 for a in attention if a["kind"] == "disbursement"),
            "total": len(attention),
        },
        "subscriptions": {
            **subs, **expiring,
            "reminders_due": sum(1 for c in clients if c["reminder_due"]),
        },
        "clients": {
            "total": len(clients),
            "new_in_range": sum(1 for c in clients if c["created_at"] and start <= c["created_at"] < end),
        },
        "inventory": {
            "total": int(inv[0]), "registered": int(inv[1]), "in_stock": int(inv[2]), "sold": int(inv[3]),
            "online": int(inv[4]), "never_connected": int(inv[5]),
            "offline": int(inv[0]) - int(inv[4]) - int(inv[5]),
        },
        "usage": {
            "points": int(points[0]), "active_devices": int(points[1]),
            "trips": int(trips[0]), "distance_km": round(float(trips[1]), 1),
        },
    })


# ── Revenue series ───────────────────────────────────────────────────────────

@router.get("/revenue")
async def revenue(
    date_from: Optional[date] = Query(None, alias="from"),
    date_to: Optional[date] = Query(None, alias="to"),
    granularity: str = Query("day", pattern="^(day|week|month)$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    now = datetime.utcnow()
    from_day, to_day, start, end = _date_range(date_from, date_to, now)
    pb = _bucket_expr(db, Payment.created_at, granularity).label("bucket")
    pay_rows = db.query(
        pb,
        func.coalesce(func.sum(case((and_(Payment.status == "successful", Payment.currency == CURRENCY), Payment.amount), else_=0)), 0),
        func.coalesce(func.sum(case((Payment.status == "successful", 1), else_=0)), 0),
        func.coalesce(func.sum(case((Payment.status == "pending", 1), else_=0)), 0),
        func.coalesce(func.sum(case((Payment.status == "failed", 1), else_=0)), 0),
    ).filter(Payment.created_at >= start, Payment.created_at < end).group_by(pb).all()
    db_ = _bucket_expr(db, Disbursement.created_at, granularity).label("bucket")
    refund_rows = db.query(db_, func.coalesce(func.sum(Disbursement.amount), 0)).filter(
        Disbursement.status == "successful", Disbursement.reference_payment_id.isnot(None),
        Disbursement.currency == CURRENCY,
        Disbursement.created_at >= start, Disbursement.created_at < end,
    ).group_by(db_).all()

    by_bucket = {k: {"bucket": k, "gross": 0.0, "refunds": 0.0, "net": 0.0,
                     "successful": 0, "pending": 0, "failed": 0}
                 for k in _bucket_keys(from_day, to_day, granularity)}
    for b, gross, ok, pending, failed in pay_rows:
        row = by_bucket.setdefault(str(b), {"bucket": str(b), "gross": 0.0, "refunds": 0.0, "net": 0.0,
                                            "successful": 0, "pending": 0, "failed": 0})
        row.update(gross=float(gross), successful=int(ok), pending=int(pending), failed=int(failed))
    for b, amount in refund_rows:
        row = by_bucket.setdefault(str(b), {"bucket": str(b), "gross": 0.0, "refunds": 0.0, "net": 0.0,
                                            "successful": 0, "pending": 0, "failed": 0})
        row["refunds"] = float(amount)
    series = sorted(by_bucket.values(), key=lambda r: r["bucket"])
    for row in series:
        row["net"] = row["gross"] - row["refunds"]
    return {
        "range": {"from": from_day.isoformat(), "to": to_day.isoformat(), "timezone": TIMEZONE},
        "granularity": granularity, "currency": CURRENCY, "series": series,
    }


# ── Payments ─────────────────────────────────────────────────────────────────

PAYMENT_SORTS = {"created_at", "amount", "status", "client_name", "plan_name"}


@router.get("/payments")
async def payments(
    status: Optional[str] = Query(None, pattern="^(successful|pending|failed)$"),
    date_from: Optional[date] = Query(None, alias="from"),
    date_to: Optional[date] = Query(None, alias="to"),
    q: Optional[str] = None,
    plan: Optional[str] = None,
    purpose: Optional[str] = Query(None, pattern="^(subscribe|add_vehicles)$"),
    user_id: Optional[int] = None,
    sort: str = "created_at",
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    format: str = Query("json", pattern="^(json|csv)$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Every payment attempt, newest first. Without from/to, all time."""
    if sort not in PAYMENT_SORTS:
        raise HTTPException(status_code=422, detail=f"sort must be one of {sorted(PAYMENT_SORTS)}")
    now = datetime.utcnow()
    query = (
        db.query(Payment, User, SubscriptionPlan)
        .outerjoin(User, User.clerk_user_id == Payment.clerk_user_id)
        .outerjoin(SubscriptionPlan, SubscriptionPlan.id == Payment.plan_id)
    )
    if status:
        query = query.filter(Payment.status == status)
    if date_from or date_to:
        _, _, start, end = _date_range(date_from, date_to, now, default_days=36500)
        query = query.filter(Payment.created_at >= start, Payment.created_at < end)
    if plan:
        query = query.filter(SubscriptionPlan.slug == plan)
    if purpose:
        query = query.filter(Payment.purpose == purpose)
    if user_id is not None:
        query = query.filter(User.id == user_id)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(
            Payment.tx_ref.ilike(like), User.email.ilike(like), User.first_name.ilike(like),
            User.last_name.ilike(like), User.phone_number.ilike(like),
        ))
    order_col = {
        "created_at": Payment.created_at, "amount": Payment.amount, "status": Payment.status,
        "client_name": User.first_name, "plan_name": SubscriptionPlan.name,
    }[sort]
    query = query.order_by(order_col.desc() if order == "desc" else order_col.asc(), Payment.id.desc())

    total = query.count()
    totals = query.with_entities(
        func.coalesce(func.sum(case((and_(Payment.status == "successful", Payment.currency == CURRENCY), Payment.amount), else_=0)), 0),
        func.coalesce(func.sum(case((Payment.status == "successful", 1), else_=0)), 0),
    ).order_by(None).one()
    rows = query.all() if format == "csv" else query.offset((page - 1) * page_size).limit(page_size).all()
    refunded = _refunded_by_payment(db, [p.id for p, _, _ in rows])

    items = []
    for p, u, pl in rows:
        items.append({
            "id": p.id, "tx_ref": p.tx_ref, "amount": p.amount, "currency": p.currency,
            "status": p.status, "purpose": p.purpose,
            "created_at": p.created_at, "verified_at": p.verified_at, "consumed_at": p.consumed_at,
            "subscription_id": p.subscription_id,
            "activated": p.consumed_at is not None,
            "refunded_amount": refunded.get(p.id, 0.0),
            "plan_slug": pl.slug if pl else None, "plan_name": pl.name if pl else None,
            "user_id": u.id if u else None, "clerk_user_id": p.clerk_user_id,
            "client_name": _display_name(u), "client_email": u.email if u else None,
            "client_phone": u.phone_number if u else None,
            "account_deleted": u is None,
            "age_minutes": int((now - p.created_at).total_seconds() // 60) if p.status == "pending" and p.created_at else None,
        })

    if format == "csv":
        return _csv_response(
            "payments.csv",
            ["id", "tx_ref", f"created ({TIMEZONE})", f"resolved ({TIMEZONE})", "status", "amount", "currency",
             "refunded", "plan", "purpose", "activated", "client", "email", "phone"],
            ([i["id"], i["tx_ref"], _kigali_text(i["created_at"]), _kigali_text(i["verified_at"]), i["status"],
              i["amount"], i["currency"], i["refunded_amount"], i["plan_name"], i["purpose"],
              "yes" if i["activated"] else "no", i["client_name"] or "(deleted account)",
              i["client_email"], i["client_phone"]] for i in items),
        )
    return {
        "items": _jsonable(items), "total": total, "page": page, "page_size": page_size,
        "totals": {"successful_amount": float(totals[0]), "successful_count": int(totals[1]), "currency": CURRENCY},
    }


@router.get("/attention")
async def attention(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Payments and payouts a human should look at, newest first."""
    items = _attention_items(db, datetime.utcnow())
    return {"items": _jsonable(items), "total": len(items)}


# ── Clients ──────────────────────────────────────────────────────────────────

CLIENT_SORTS = {
    "name", "email", "status", "plan_name", "expires_at", "days_remaining", "net_paid", "total_paid",
    "last_payment_at", "devices_total", "vehicles_total", "created_at",
}


@router.get("/clients")
async def clients(
    q: Optional[str] = None,
    status: Optional[str] = Query(None, pattern="^(active|expiring|expired|cancelled|none)$"),
    within: Optional[int] = Query(None, ge=1, le=365),
    reminder: Optional[str] = Query(None, pattern="^(due|sent|not_sent)$"),
    plan: Optional[str] = None,
    sort: str = "days_remaining",
    order: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    format: str = Query("json", pattern="^(json|csv)$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    One row per client. status=expiring means active and expiring within
    `within` days (default 14). reminder: due = a reminder is due now but not
    sent yet; sent = at least one reminder went out for the current expiry
    date; not_sent = none did.
    """
    if sort not in CLIENT_SORTS:
        raise HTTPException(status_code=422, detail=f"sort must be one of {sorted(CLIENT_SORTS)}")
    now = datetime.utcnow()
    rows = _client_rows(db, now)

    window = within or (14 if status == "expiring" else None)
    out = []
    for r in rows:
        if not _matches(q, r["name"], r["email"], r["phone_number"], r["plan_name"]):
            continue
        if status == "expiring":
            if r["status"] != "active" or r["days_remaining"] is None or r["days_remaining"] > window:
                continue
        elif status and r["status"] != status:
            continue
        elif window and status in (None, "active") and (r["days_remaining"] is None or r["days_remaining"] > window):
            continue
        if plan and r["plan_slug"] != plan:
            continue
        if reminder == "due" and not r["reminder_due"]:
            continue
        if reminder == "sent" and not r["reminders_sent"]:
            continue
        if reminder == "not_sent" and r["reminders_sent"]:
            continue
        out.append(r)
    out = _sorted(out, sort, order == "desc")

    if format == "csv":
        return _csv_response(
            "clients.csv",
            ["user_id", "name", "email", "phone", "plan", "status", f"expires ({TIMEZONE})", "days_remaining",
             "last_reminder", f"last_reminder_at ({TIMEZONE})", "last_reminder_email_sent", "reminder_due",
             "vehicles", "vehicles_covered", "devices", "devices_online",
             "total_paid_rwf", "refunded_rwf", "net_paid_rwf", f"last_payment ({TIMEZONE})"],
            ([r["user_id"], r["name"], r["email"], r["phone_number"], r["plan_name"], r["status"],
              _kigali_text(r["expires_at"]),
              None if r["days_remaining"] is None else math.floor(r["days_remaining"] * 10) / 10,
              r["last_reminder"]["stage"] if r["last_reminder"] else None,
              _kigali_text(r["last_reminder"]["sent_at"]) if r["last_reminder"] else None,
              ({True: "yes", False: "no"}.get(r["last_reminder"]["email_sent"], "unknown")
               if r["last_reminder"] else None),
              r["reminder_due"], r["vehicles_total"], r["vehicles_covered"], r["devices_total"],
              r["devices_online"], r["total_paid"], r["total_refunded"], r["net_paid"],
              _kigali_text(r["last_payment_at"])] for r in out),
        )
    return _page(out, page, page_size)


@router.get("/clients/{user_id}")
async def client_detail(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    now = datetime.utcnow()
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    summary = _client_rows(db, now, only_user_id=user_id)[0]
    clerk = user.clerk_user_id
    current_sub_id = summary["subscription_id"]

    subs = (
        db.query(Subscription).options(joinedload(Subscription.plan))
        .filter(Subscription.clerk_user_id == clerk)
        .order_by(Subscription.created_at.desc(), Subscription.id.desc()).all()
    )
    sub_ids = [s.id for s in subs]
    reminders = (
        db.query(SubscriptionReminder).filter(SubscriptionReminder.subscription_id.in_(sub_ids))
        .order_by(SubscriptionReminder.sent_at.desc()).all()
    ) if sub_ids else []
    covered_counts = dict(
        db.query(SubscriptionVehicle.subscription_id, func.count(SubscriptionVehicle.id))
        .filter(SubscriptionVehicle.subscription_id.in_(sub_ids))
        .group_by(SubscriptionVehicle.subscription_id).all()
    ) if sub_ids else {}
    covered_vehicle_ids = {
        vid for (vid,) in db.query(SubscriptionVehicle.vehicle_id)
        .filter(SubscriptionVehicle.subscription_id == current_sub_id)
    } if current_sub_id and summary["status"] == "active" else set()

    devices = db.query(Device).filter(Device.user_id == user.id).all()
    vehicles = db.query(Vehicle).filter(Vehicle.clerk_user_id == clerk).order_by(Vehicle.created_at).all()
    device_ids = [d.id for d in devices]
    since = now - timedelta(days=30)
    points = dict(
        db.query(Location.device_id, func.count(Location.id))
        .filter(Location.device_id.in_(device_ids), Location.timestamp >= since)
        .group_by(Location.device_id).all()
    ) if device_ids else {}
    trips = {
        r[0]: r for r in db.query(Trip.device_id, func.count(Trip.id), func.coalesce(func.sum(Trip.total_distance_km), 0))
        .filter(Trip.device_id.in_(device_ids), Trip.start_time >= since).group_by(Trip.device_id).all()
    } if device_ids else {}
    vehicle_by_device = {v.device_id: v for v in vehicles if v.device_id}

    payments_rows = (
        db.query(Payment).options(joinedload(Payment.plan))
        .filter(Payment.clerk_user_id == clerk).order_by(Payment.created_at.desc()).all()
    )
    refunded = _refunded_by_payment(db, [p.id for p in payments_rows])
    disbursements = (
        db.query(Disbursement).filter(Disbursement.clerk_user_id == clerk)
        .order_by(Disbursement.created_at.desc()).all()
    )

    return _jsonable({
        "generated_at": now,
        "timezone": TIMEZONE,
        "client": summary,
        "subscriptions": [{
            "id": s.id, "plan_slug": s.plan.slug if s.plan else None, "plan_name": s.plan.name if s.plan else None,
            "status": _effective_status(s, now), "stored_status": s.status, "price": s.price,
            "quantity": s.quantity, "vehicles_covered": int(covered_counts.get(s.id, 0)),
            "started_at": s.started_at, "expires_at": s.expires_at, "created_at": s.created_at,
            "is_current": s.id == current_sub_id,
            "reminders": [{
                "stage": r.stage, "expires_at": r.expires_at, "sent_at": r.sent_at,
                "email_sent": r.email_sent, "push_sent": r.push_sent,
            } for r in reminders if r.subscription_id == s.id],
        } for s in subs],
        "vehicles": [{
            "id": v.id, "nickname": v.nickname, "plate": v.plate, "make": v.make, "model": v.model,
            "device_id": v.device_id, "covered": v.id in covered_vehicle_ids, "created_at": v.created_at,
        } for v in vehicles],
        "devices": [{
            "id": d.id, "imei": d.imei, "name": d.name, "hardware_model": d.hardware_model,
            "sim_number": d.sim_number, "lifecycle": d.lifecycle,
            "connectivity": _connectivity(d.last_update, now), "last_update": d.last_update,
            "position_confirmed_at": d.position_confirmed_at,
            "vehicle": ({"id": vehicle_by_device[d.id].id, "nickname": vehicle_by_device[d.id].nickname,
                         "plate": vehicle_by_device[d.id].plate} if d.id in vehicle_by_device else None),
            "points_30d": int(points.get(d.id, 0)),
            "trips_30d": int(trips[d.id][1]) if d.id in trips else 0,
            "distance_km_30d": round(float(trips[d.id][2]), 1) if d.id in trips else 0.0,
        } for d in devices],
        "payments": [{
            "id": p.id, "tx_ref": p.tx_ref, "amount": p.amount, "currency": p.currency, "status": p.status,
            "purpose": p.purpose, "plan_name": p.plan.name if p.plan else None,
            "created_at": p.created_at, "verified_at": p.verified_at,
            "activated": p.consumed_at is not None, "subscription_id": p.subscription_id,
            "refunded_amount": refunded.get(p.id, 0.0),
        } for p in payments_rows],
        "disbursements": [{
            "id": d.id, "tx_ref": d.tx_ref, "amount": d.amount, "currency": d.currency, "status": d.status,
            "reason": d.reason, "phone": d.phone, "reference_payment_id": d.reference_payment_id,
            "created_at": d.created_at, "verified_at": d.verified_at,
        } for d in disbursements],
    })


# ── Inventory ────────────────────────────────────────────────────────────────

INVENTORY_SORTS = {"name", "imei", "lifecycle", "connectivity", "last_update", "owner_name", "created_at"}


def _inventory_rows(db: Session, now: datetime) -> list[dict]:
    devices = db.query(Device, User).outerjoin(User, User.id == Device.user_id).all()
    rn = func.row_number().over(
        partition_by=Vehicle.device_id, order_by=(Vehicle.created_at.desc(), Vehicle.id.desc())).label("rn")
    inner = select(Vehicle.id.label("id"), rn).where(Vehicle.device_id.isnot(None)).subquery()
    latest_vehicle_ids = [r.id for r in db.execute(select(inner.c.id).where(inner.c.rn == 1))]
    vehicles = {
        v.device_id: v for v in db.query(Vehicle).filter(Vehicle.id.in_(latest_vehicle_ids)).all()
    } if latest_vehicle_ids else {}
    subs = _latest_subscriptions(db)
    active_sub_ids = [s.id for s in subs.values() if _effective_status(s, now) == "active"]
    covered = {
        vid for (vid,) in db.query(SubscriptionVehicle.vehicle_id)
        .filter(SubscriptionVehicle.subscription_id.in_(active_sub_ids))
    } if active_sub_ids else set()

    rows = []
    for d, u in devices:
        v = vehicles.get(d.id)
        sub = subs.get(u.clerk_user_id) if u else None
        conn = _connectivity(d.last_update, now)
        rows.append({
            "id": d.id, "imei": d.imei, "name": d.name, "hardware_model": d.hardware_model,
            "sim_number": d.sim_number, "sim_renewal_date": d.sim_renewal_date,
            "lifecycle": d.lifecycle, "sold": d.user_id is not None,
            "connectivity": conn, "last_update": d.last_update, "last_connect": d.last_connect,
            "offline_minutes": int((now - d.last_update).total_seconds() // 60) if conn == "offline" else None,
            "battery_level": d.battery_level, "gsm_signal": d.gsm_signal,
            "owner_id": u.id if u else None, "owner_name": _display_name(u), "owner_email": u.email if u else None,
            "vehicle_id": v.id if v else None, "vehicle_nickname": v.nickname if v else None,
            "vehicle_plate": v.plate if v else None,
            "covered": bool(v and v.id in covered),
            "plan_name": sub.plan.name if sub and sub.plan else None,
            "plan_status": _effective_status(sub, now) if u else None,
            "created_at": d.created_at,
        })
    return rows


@router.get("/inventory")
async def inventory(
    q: Optional[str] = None,
    lifecycle: Optional[str] = Query(None, pattern="^(registered|in_stock|sold)$"),
    connectivity: Optional[str] = Query(None, pattern="^(online|offline|never)$"),
    sort: str = "name",
    order: str = Query("asc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    format: str = Query("json", pattern="^(json|csv)$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    if sort not in INVENTORY_SORTS:
        raise HTTPException(status_code=422, detail=f"sort must be one of {sorted(INVENTORY_SORTS)}")
    now = datetime.utcnow()
    rows = [
        r for r in _inventory_rows(db, now)
        if _matches(q, r["name"], r["imei"], r["sim_number"], r["owner_name"], r["owner_email"],
                    r["vehicle_plate"], r["vehicle_nickname"])
        and (not lifecycle or r["lifecycle"] == lifecycle)
        and (not connectivity or r["connectivity"] == connectivity)
    ]
    rows = _sorted(rows, sort, order == "desc")
    if format == "csv":
        return _csv_response(
            "gps-inventory.csv",
            ["id", "imei", "name", "model", "sim", "lifecycle", "connectivity", f"last_seen ({TIMEZONE})",
             "owner", "owner_email", "vehicle", "plate", "covered_by_plan", "plan", "plan_status"],
            ([r["id"], r["imei"], r["name"], r["hardware_model"], r["sim_number"], r["lifecycle"],
              r["connectivity"], _kigali_text(r["last_update"]), r["owner_name"], r["owner_email"],
              r["vehicle_nickname"], r["vehicle_plate"], "yes" if r["covered"] else "no",
              r["plan_name"], r["plan_status"]] for r in rows),
        )
    return _page(rows, page, page_size)


# ── Usage ────────────────────────────────────────────────────────────────────

@router.get("/usage")
async def usage(
    date_from: Optional[date] = Query(None, alias="from"),
    date_to: Optional[date] = Query(None, alias="to"),
    q: Optional[str] = None,
    format: str = Query("json", pattern="^(json|csv)$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    GPS usage per device and per client over a Kigali-date range (default
    last 30 days): GPS fixes received (locations rows — the backend stores
    no byte counts, so this is the data-volume measure), valid fixes, trips
    and trip distance. Uses the (device_id, timestamp) index.
    """
    now = datetime.utcnow()
    from_day, to_day, start, end = _date_range(date_from, date_to, now)
    points = {
        r[0]: r for r in db.query(
            Location.device_id, func.count(Location.id),
            func.coalesce(func.sum(case((Location.gps_valid.is_(True), 1), else_=0)), 0),
            func.max(Location.timestamp),
        ).filter(Location.timestamp >= start, Location.timestamp < end).group_by(Location.device_id).all()
    }
    trips = {
        r[0]: r for r in db.query(Trip.device_id, func.count(Trip.id), func.coalesce(func.sum(Trip.total_distance_km), 0))
        .filter(Trip.start_time >= start, Trip.start_time < end).group_by(Trip.device_id).all()
    }
    devices = [
        r for r in _inventory_rows(db, now)
        if _matches(q, r["name"], r["imei"], r["owner_name"], r["owner_email"], r["vehicle_plate"])
    ]
    device_rows = []
    for d in devices:
        p, t = points.get(d["id"]), trips.get(d["id"])
        device_rows.append({
            "device_id": d["id"], "imei": d["imei"], "name": d["name"],
            "owner_id": d["owner_id"], "owner_name": d["owner_name"],
            "vehicle_plate": d["vehicle_plate"], "connectivity": d["connectivity"],
            "last_update": d["last_update"],
            "points": int(p[1]) if p else 0, "valid_points": int(p[2]) if p else 0,
            "last_point_in_range": _dt(p[3]) if p else None,
            "trips": int(t[1]) if t else 0, "distance_km": round(float(t[2]), 1) if t else 0.0,
        })
    device_rows = _sorted(device_rows, "points", True)

    clients: dict[Any, dict] = {}
    for r in device_rows:
        if r["owner_id"] is None:
            continue
        c = clients.setdefault(r["owner_id"], {
            "user_id": r["owner_id"], "name": r["owner_name"], "devices": 0, "points": 0,
            "trips": 0, "distance_km": 0.0, "last_update": None,
        })
        c["devices"] += 1
        c["points"] += r["points"]
        c["trips"] += r["trips"]
        c["distance_km"] = round(c["distance_km"] + r["distance_km"], 1)
        if r["last_update"] and (c["last_update"] is None or r["last_update"] > c["last_update"]):
            c["last_update"] = r["last_update"]

    if format == "csv":
        return _csv_response(
            f"gps-usage_{from_day}_{to_day}.csv",
            ["device_id", "imei", "name", "owner", "plate", "connectivity", f"last_seen ({TIMEZONE})",
             "gps_fixes", "valid_fixes", "trips", "distance_km"],
            ([r["device_id"], r["imei"], r["name"], r["owner_name"], r["vehicle_plate"], r["connectivity"],
              _kigali_text(r["last_update"]), r["points"], r["valid_points"], r["trips"], r["distance_km"]]
             for r in device_rows),
        )
    return _jsonable({
        "range": {"from": from_day, "to": to_day, "timezone": TIMEZONE},
        "devices": device_rows,
        "clients": _sorted(list(clients.values()), "points", True),
    })

"""Device API endpoints"""

import logging
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload
from datetime import datetime, timedelta
import secrets
import string
import time
from collections import defaultdict

from app.core.database import get_db
from app.core.auth import require_auth, require_admin, get_current_user, require_device_access
from app.models.alert_settings import AlertSettings
from app.models.device import Device
from app.models.location import Location
from app.models.trip import Trip
from app.models.vehicle import Vehicle
from app.models.subscription import SubscriptionPlan, Subscription, Payment
from app.models.company import Membership
from app.api.trips import TripResponse
from app.api.subscriptions import SubscriptionPlanResponse
from app.core.config import settings
from app.models.user import User, Role
from pydantic import BaseModel, field_validator, model_validator

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Simple in-memory rate limiter for pairing endpoint ────────────────────────
_pair_attempts: Dict[str, list] = defaultdict(list)  # ip -> [timestamps]
_PAIR_WINDOW_SECONDS = 60
_PAIR_MAX_ATTEMPTS   = 5

def _check_pair_rate_limit(ip: str):
    """Raise 429 if the IP has exceeded pairing attempt limits."""
    now = time.monotonic()
    cutoff = now - _PAIR_WINDOW_SECONDS
    # Keep only recent attempts
    _pair_attempts[ip] = [t for t in _pair_attempts[ip] if t > cutoff]
    if len(_pair_attempts[ip]) >= _PAIR_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail=f"Too many pairing attempts. Please wait {_PAIR_WINDOW_SECONDS} seconds and try again."
        )
    _pair_attempts[ip].append(now)


VALID_MARKER_ICONS = {"arrow", "sedan", "truck_small", "truck_big", "bus", "animal", "motorcycle"}


# Pydantic schemas
class DeviceBase(BaseModel):
    name: str
    description: Optional[str] = None
    sim_number: Optional[str] = None  # SIM card phone number (for SMS configuration)
    hardware_model: Optional[str] = None
    sim_renewal_date: Optional[datetime] = None


class DeviceCreate(DeviceBase):
    imei: str
    pairing_pin: Optional[str] = None  # Auto-generated if omitted


class DeviceUpdate(DeviceBase):
    pass


class DeviceAssignRequest(BaseModel):
    """Body for `POST /{device_id}/assign` — assign a device to a company.

    Admin-only endpoint. The target company must exist. A device may be
    assigned to at most one company at a time.
    """

    company_id: int


class DeviceMarkerIconUpdate(BaseModel):
    """Body for `PATCH /{device_id}` — a lightweight partial update covering
    only marker_icon. PUT /{device_id} (DeviceUpdate) isn't a true partial
    update: it requires resending `name` on every call, so it's a poor fit
    for "just change the marker glyph"."""

    marker_icon: str

    @field_validator("marker_icon")
    @classmethod
    def marker_icon_valid(cls, v: str) -> str:
        if v not in VALID_MARKER_ICONS:
            raise ValueError(
                f"marker_icon must be one of: {', '.join(sorted(VALID_MARKER_ICONS))}"
            )
        return v


class DeviceSubscriptionInfo(BaseModel):
    """A device's payment scheme / subscription mode.

    status:
      active   — the owner has an unexpired subscription for this scheme
      expired  — a subscription exists but has lapsed (or was cancelled)
      none     — no subscription record at all (trial pending or unpaid)

    `plan_slug` / `plan_name` / `billing_type` describe the scheme the
    subscription was bought under (e.g. 'basic' / 'Basic' / 'recurrent');
    they resolve to None when the plan was deleted or the slug is unknown.
    """

    status: str = "none"
    plan_slug: Optional[str] = None
    plan_name: Optional[str] = None
    billing_type: Optional[str] = None
    started_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class DeviceResponse(DeviceBase):
    id: int
    imei: str
    pairing_pin: Optional[str]  # Shown on creation; used during mobile pairing
    lifecycle: str              # registered | in_stock | sold
    status: str
    user_id: Optional[int]
    plan_id: Optional[int] = None
    plan: Optional[SubscriptionPlanResponse] = None  # Linked subscription scheme
    subscription: Optional[DeviceSubscriptionInfo] = None  # Owner's payment/subscription mode
    last_connect: Optional[datetime]
    last_update: Optional[datetime]
    last_latitude: Optional[float]
    last_longitude: Optional[float]
    battery_level: Optional[int]
    gsm_signal: Optional[int]
    created_at: datetime
    marker_icon: str  # arrow (default) | sedan | truck_small | truck_big | bus | animal | motorcycle
    # Aggregate fields against the trips table — only populated by
    # list_devices (see its trip_count_subq/last_trip_at_subq), so the
    # Flutter selector can show them without an N+1 call per device.
    # Default to 0/None so every other endpoint returning a bare Device ORM
    # object (get_device, create_device, etc.) keeps working unchanged —
    # Pydantic falls back to these defaults for a Device instance that
    # never had trip_count/last_trip_at set on it.
    trip_count: int = 0
    last_trip_at: Optional[datetime] = None
    # Registration-time vehicle info — lives on the separate `vehicles`
    # table (see app/models/vehicle.py), not on Device itself, since it's
    # captured by the mobile app's device-pairing flow (POST /api/vehicles)
    # as a distinct step, not device creation. None whenever a device has
    # no linked Vehicle row (e.g. in_stock/unpaired devices, or ones
    # created directly via POST /{device_id} without ever going through
    # onboarding). Populated by list_devices/get_device/get_device_by_imei
    # via `_apply_vehicle_info` — same transient-attribute,
    # avoid-N+1-via-batch-load pattern as trip_count/last_trip_at above.
    nickname: Optional[str] = None
    plate: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None

    class Config:
        from_attributes = True


class LocationIntervalStats(BaseModel):
    samples: int
    avg_seconds: Optional[float]
    min_seconds: Optional[float]
    max_seconds: Optional[float]
    last_interval_seconds: Optional[float]


class DeviceDiagnosticsResponse(BaseModel):
    device_id: int
    imei: str
    status: str
    last_connect: Optional[datetime]
    last_update: Optional[datetime]
    last_location_timestamp: Optional[datetime]
    seconds_since_last_update: Optional[int]
    sending_status: str
    location_intervals: LocationIntervalStats


class DevicePlanUpdate(BaseModel):
    """Body for `PUT /{device_id}/plan` — link a subscription scheme to a device.
    Send `plan_id: null` to unlink the device from its plan."""

    plan_id: Optional[int] = None


class DeviceOwnerInfo(BaseModel):
    """The company a device is assigned to."""

    company_id: int
    name: str
    is_company: bool





class PaymentInfo(BaseModel):
    """A single verified payment (IntouchPay) belonging to the device owner."""

    tx_ref: str
    plan_id: str
    amount: float
    currency: str
    status: str
    verified_at: Optional[datetime]


class DeviceBillingResponse(BaseModel):
    """Full billing picture for ONE device: the linked scheme (plan), the
    owner's current subscription state, and the payment history that matches
    this device's plan."""

    device_id: int
    imei: str
    name: str
    lifecycle: str
    status: str
    owner: Optional[DeviceOwnerInfo] = None
    plan: Optional[SubscriptionPlanResponse] = None
    subscription: Optional[DeviceSubscriptionInfo] = None
    payments: List[PaymentInfo] = []


# ── Shared billing helpers ────────────────────────────────────────────────────

def _plans_by_slug(db: Session) -> dict:
    """slug (lowercased) -> SubscriptionPlan — for resolving a subscription's
    plan_id slug to a name/billing_type without N+1 lookups."""
    return {p.slug.lower(): p for p in db.query(SubscriptionPlan).all()}


def _latest_subscription(db: Session, clerk_user_id: str) -> Optional[Subscription]:
    """Most recently created subscription for a user (any status).
    Used to derive the current subscription mode for their devices."""
    if not clerk_user_id:
        return None
    return (
        db.query(Subscription)
        .filter(Subscription.clerk_user_id == clerk_user_id)
        .order_by(Subscription.created_at.desc())
        .first()
    )


def _build_subscription_info(
    sub: Optional[Subscription], plans_by_slug: dict
) -> DeviceSubscriptionInfo:
    """Derive a DeviceSubscriptionInfo from a Subscription row."""
    if sub is None:
        return DeviceSubscriptionInfo(status="none")
    plan = plans_by_slug.get((sub.plan_id or "").lower())
    now = datetime.utcnow()
    active = sub.status == "active" and sub.expires_at and sub.expires_at > now
    return DeviceSubscriptionInfo(
        status="active" if active else "expired",
        plan_slug=sub.plan_id,
        plan_name=plan.name if plan else None,
        billing_type=plan.billing_type if plan else None,
        started_at=sub.started_at,
        expires_at=sub.expires_at,
    )


# ── Vehicle-info helpers (nickname/plate/make/model, DeviceResponse) ─────────
# `Vehicle` is a separate table (captured by the mobile app's device-pairing
# flow, POST /api/vehicles — see app/api/onboarding.py) linked by
# `Vehicle.device_id -> Device.id`. Nothing enforces at most one Vehicle per
# device (no unique constraint on vehicles.device_id), so "latest wins" —
# same resolution `_latest_subscription`/`latest_sub.setdefault` already use
# for the analogous one-user-many-subscriptions case above.

def _latest_vehicle_by_device_id(db: Session, device_ids) -> dict:
    """device_id -> most-recently-created Vehicle, batched for a whole page
    of devices in one query — the list_devices equivalent of
    `_latest_subscription`, sized to avoid an N+1 per row."""
    if not device_ids:
        return {}
    vehicles = (
        db.query(Vehicle)
        .filter(Vehicle.device_id.in_(device_ids))
        .order_by(Vehicle.created_at.desc())
        .all()
    )
    by_device_id: dict = {}
    for vehicle in vehicles:
        by_device_id.setdefault(vehicle.device_id, vehicle)
    return by_device_id


def _apply_vehicle_info(device: Device, vehicle: Optional[Vehicle]) -> None:
    """Sets DeviceResponse's nickname/plate/make/model as transient
    attributes on `device` — same `from_attributes`-reads-anything trick
    trip_count/subscription already rely on. `vehicle=None` (no linked
    registration yet) leaves all four as None, which DeviceResponse already
    defaults to."""
    device.nickname = vehicle.nickname if vehicle else None
    device.plate = vehicle.plate if vehicle else None
    device.make = vehicle.make if vehicle else None
    device.model = vehicle.model if vehicle else None


@router.get("/", response_model=List[DeviceResponse])
async def list_devices(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    status: Optional[str] = Query(None, description="Filter by status: online, offline"),
    company_id: Optional[int] = Query(
        None, description="Filter devices by company"
    ),
    lifecycle: Optional[str] = Query(
        None, description="Filter by lifecycle: registered, in_stock, sold"
    ),
    db: Session = Depends(get_db),
    clerk_user_id: str = Depends(require_auth),
):
    """List GPS tracker devices.

    - Non-admin callers only see devices belonging to companies they are
      a member of.
    - Admins see everything and may additionally filter by `company_id`
      and/or `lifecycle`.
    """
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    is_admin = user.role in (Role.SUPER_ADMIN, Role.ADMIN)

    # Correlated scalar subqueries rather than a JOIN + GROUP BY: a device
    # with zero trips would be dropped by an inner join (or need an outer
    # join anyway), and this keeps the existing single-table Device query
    # untouched below — just two extra selected columns.
    trip_count_subq = (
        db.query(func.count(Trip.id))
        .filter(Trip.device_id == Device.id)
        .correlate(Device)
        .scalar_subquery()
    )
    last_trip_at_subq = (
        db.query(func.max(Trip.start_time))
        .filter(Trip.device_id == Device.id)
        .correlate(Device)
        .scalar_subquery()
    )

    query = db.query(Device, trip_count_subq, last_trip_at_subq)
    # Eager-load the linked plan so DeviceResponse.plan doesn't trigger an
    # N+1 lazy-load per device row.
    query = query.options(selectinload(Device.plan))

    if not is_admin:
        # Non-admin: only devices belonging to companies the user is a member of
        member_company_ids = [
            m.company_id for m in
            db.query(Membership.company_id).filter(Membership.user_id == user.id).all()
        ]
        if not member_company_ids:
            # User belongs to no companies — return empty list
            return []
        query = query.filter(Device.company_id.in_(member_company_ids))
    elif company_id is not None:
        # Admin filtering by specific company
        query = query.filter(Device.company_id == company_id)

    if status:
        query = query.filter(Device.status == status)

    if lifecycle:
        query = query.filter(Device.lifecycle == lifecycle)

    rows = query.offset(skip).limit(limit).all()

    logger.info(
        "Device list: user_id=%s role=%s admin=%s | total devices in DB=%s | "
        "returned=%s",
        user.id, user.role, is_admin, db.query(func.count(Device.id)).scalar(),
        len(rows),
    )

    # Batch-load the subscription mode for every device's company owner
    try:
        company_ids = {d[0].company_id for d in rows if d[0].company_id}
        companies_by_id = {}
        if company_ids:
            from app.models.company import Company
            companies_by_id = {c.id: c for c in db.query(Company).filter(Company.id.in_(company_ids)).all()}
        plans_by_slug = _plans_by_slug(db)
    except SQLAlchemyError as e:
        db.rollback()
        logger.warning(
            "Could not load company info for the device list — returning "
            "devices without subscription data (%s: %s).",
            type(e).__name__, e,
        )
        companies_by_id = {}
        plans_by_slug = {}

    try:
        vehicle_by_device_id = _latest_vehicle_by_device_id(db, {d[0].id for d in rows})
    except SQLAlchemyError as e:
        logger.warning(
            "Could not load vehicle info for the device list — returning "
            "devices without nickname/plate/make/model (%s: %s).",
            type(e).__name__, e,
        )
        vehicle_by_device_id = {}

    devices = []
    for device, trip_count, last_trip_at in rows:
        device.trip_count = trip_count or 0
        device.last_trip_at = last_trip_at
        device.subscription = DeviceSubscriptionInfo(status="none")  # simplified for now
        _apply_vehicle_info(device, vehicle_by_device_id.get(device.id))
        devices.append(device)
    return devices


@router.get("/rejected")
async def list_rejected_devices(
    request: Request,
    db: Session = Depends(get_db),
    clerk_user_id: str = Depends(require_auth),
):
    """Get the list of recently rejected unknown IMEI connections."""
    tcp_server = getattr(request.app.state, "tcp_server", None)
    if not tcp_server:
        return []
    
    # Get all currently registered IMEIs
    registered_imeis = {row[0] for row in db.query(Device.imei).all()}
    
    rejected = []
    # Iterate over a copy since we might mutate the original list later
    for r in list(tcp_server.rejected_imeis):
        if r["imei"] in registered_imeis:
            continue  # Skip devices that have already been registered!
            
        diff = int((datetime.utcnow() - r["time"]).total_seconds())
        rejected.append({
            "imei": r["imei"],
            "ip": r["ip"],
            "time": r["time"].isoformat(),
            "seconds_ago": diff
        })
    return rejected


@router.get("/{device_id}/billing", response_model=DeviceBillingResponse)
# Route ordering: /{device_id}/billing is declared before /{device_id}/trips
# because both are two-segment paths with a literal second segment ("billing"
# vs "trips") — FastAPI matches in declaration order, so no conflict.
# /imei/{imei} is also two-segment but with the literal "imei" prefix, so
# it does not collide with either.
async def get_device_billing(
    device_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Full billing picture for one device: the linked subscription scheme
    (plan), the owner's current subscription mode, and the payment history
    matching that plan. Owner or admin only (require_device_access).
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user, db)

    from app.models.company import Company
    company = (
        db.query(Company).filter(Company.id == device.company_id).first()
        if device.company_id else None
    )
    plan = (
        db.query(SubscriptionPlan).filter(SubscriptionPlan.id == device.plan_id).first()
        if device.plan_id else None
    )

    plans_by_slug = _plans_by_slug(db)

    # Resolve subscription via company owner's user
    sub = None
    payments = []
    if device.company_id:
        from app.models.company import Membership
        owner_member = (
            db.query(Membership)
            .filter(Membership.company_id == device.company_id)
            .order_by(Membership.created_at.asc())
            .first()
        )
        if owner_member:
            owner_user = db.query(User).filter(User.id == owner_member.user_id).first()
            if owner_user:
                sub = _latest_subscription(db, owner_user.clerk_user_id)
                pay_rows = (
                    db.query(Payment)
                    .filter(Payment.clerk_user_id == owner_user.clerk_user_id)
                    .order_by(Payment.verified_at.desc())
                    .limit(50)
                    .all()
                )
                payments = [
                    PaymentInfo(
                        tx_ref=p.tx_ref,
                        plan_id=p.plan_id,
                        amount=p.amount,
                        currency=p.currency,
                        status=p.status,
                        verified_at=p.verified_at,
                    )
                    for p in pay_rows
                ]

    return DeviceBillingResponse(
        device_id=device.id,
        imei=device.imei,
        name=device.name,
        lifecycle=device.lifecycle,
        status=device.status,
        owner=DeviceOwnerInfo(
            company_id=company.id,
            name=company.name,
            is_company=company.is_company,
        ) if company else None,
        plan=plan,
        subscription=_build_subscription_info(sub, plans_by_slug),
        payments=payments,
    )


@router.get("/{device_id}/trips", response_model=List[TripResponse])
async def list_device_trips(
    device_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List trips for a device. Access trips via device_id."""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user, db)
    trips = (
        db.query(Trip)
        .filter(Trip.device_id == device_id)
        .order_by(Trip.created_at.desc())
        .all()
    )
    return trips


@router.get("/{device_id}", response_model=DeviceResponse)
async def get_device(
    device_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get device by ID"""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user, db)

    device.subscription = DeviceSubscriptionInfo(status="none")
    _apply_vehicle_info(device, _latest_vehicle_by_device_id(db, {device.id}).get(device.id))
    return device


@router.get("/imei/{imei}", response_model=DeviceResponse)
async def get_device_by_imei(
    imei: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get device by IMEI"""
    device = db.query(Device).filter(Device.imei == imei).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user, db)
    _apply_vehicle_info(device, _latest_vehicle_by_device_id(db, {device.id}).get(device.id))
    return device


class DeviceCreateWithCompany(DeviceCreate):
    company_id: Optional[int] = None  # optional: assign directly to a company


@router.post("/", response_model=DeviceResponse, status_code=201)
async def create_device(
    device_data: DeviceCreateWithCompany,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Whitelist a new GPS device in the inventory. Admin only.
    If `pairing_pin` is not provided, a secure 6-character PIN is auto-generated.
    Optionally assign directly to a company via `company_id`.
    """
    existing = db.query(Device).filter(Device.imei == device_data.imei).first()
    if existing:
        raise HTTPException(status_code=400, detail="Device with this IMEI already exists")

    # Validate company if provided
    if device_data.company_id is not None:
        from app.models.company import Company
        company = db.query(Company).filter(Company.id == device_data.company_id).first()
        if not company:
            raise HTTPException(status_code=404, detail="Company not found")

    # Auto-generate a pairing PIN if not supplied
    pin = (device_data.pairing_pin or "").strip().upper()
    if not pin:
        alphabet = string.ascii_uppercase + string.digits
        pin = "".join(secrets.choice(alphabet) for _ in range(6))

    device = Device(
        imei=device_data.imei,
        name=device_data.name or f"Tracker-{device_data.imei[-6:]}",
        description=device_data.description,
        sim_number=device_data.sim_number,
        pairing_pin=pin,
        lifecycle='registered' if device_data.company_id is None else 'sold',
        company_id=device_data.company_id,
        status='offline'
    )

    db.add(device)
    db.commit()
    db.refresh(device)
    return device


@router.put("/{device_id}", response_model=DeviceResponse)
async def update_device(
    device_id: int,
    device_data: DeviceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Update device information"""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user, db)

    device.name = device_data.name
    if device_data.description is not None:
        device.description = device_data.description
    if device_data.sim_number is not None:
        device.sim_number = device_data.sim_number
    if device_data.hardware_model is not None:
        device.hardware_model = device_data.hardware_model
    if device_data.sim_renewal_date is not None:
        device.sim_renewal_date = device_data.sim_renewal_date
    device.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(device)

    return device


@router.put("/{device_id}/plan", response_model=DeviceResponse)
async def set_device_plan(
    device_id: int,
    body: DevicePlanUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Link (or unlink) an admin-configured subscription scheme to a device.
    Admin only.

    The plan determines what the client is billed for this device (see the
    subscription-plans API). Send `plan_id: null` to remove the link.
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if body.plan_id is not None:
        plan = db.query(SubscriptionPlan).filter(
            SubscriptionPlan.id == body.plan_id
        ).first()
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")

    device.plan_id = body.plan_id
    device.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(device)

    logger.info(
        "Device %s (id=%s) plan -> %s",
        device.imei, device.id, body.plan_id if body.plan_id is not None else "unlinked",
    )
    return device


@router.patch("/{device_id}", response_model=DeviceResponse)
async def update_device_marker_icon(
    device_id: int,
    body: DeviceMarkerIconUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Update just a device's marker_icon. Same ownership-or-admin check as
    every other device endpoint (require_device_access) — see
    DeviceMarkerIconUpdate's docstring for why this is PATCH rather than
    reusing PUT.
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user, db)

    device.marker_icon = body.marker_icon
    device.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(device)

    return device


@router.delete("/{device_id}", status_code=204)
async def delete_device(
    device_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete device"""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user, db)
    db.delete(device)
    db.commit()
    return None


# ---------------------------------------------------------------------------
# Device <-> Client assignment (admin-only)
#
# The mobile pairing flow (POST /api/devices/pair) lets a CLIENT claim a device
# themselves using IMEI + pairing PIN. These endpoints are the ADMIN-side
# counterpart: they let staff assign an inventory device to a client account
# directly (or reclaim it) without needing the PIN.
# ---------------------------------------------------------------------------


def _resolve_company(db: Session, company_id: int) -> 'Company':
    """Resolve the target company. Raises 404 on failure."""
    from app.models.company import Company
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


def _apply_assignment(device: Device, company_id: int, db: Session) -> Device:
    """
    Shared assignment logic: assign device to a company.

    Raises HTTPException(409) if the device is already assigned to a different
    company. Assigning to the same company is a no-op.
    """
    if device.company_id is not None and device.company_id != company_id:
        raise HTTPException(
            status_code=409,
            detail="Device is already assigned to another company. Unassign it first.",
        )

    device.company_id = company_id
    device.lifecycle = "sold"
    device.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(device)

    logger.info("Device %s (id=%s) assigned to company_id=%s", device.imei, device.id, company_id)
    return device


@router.post("/{device_id}/assign", response_model=DeviceResponse)
async def assign_device_to_company(
    device_id: int,
    body: DeviceAssignRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Assign an inventory device to a company. Admin only.

    Body: `{ company_id: <id> }`. The device is linked to the company and
    all company members gain access to it.

    A device can belong to at most one company: 409 if already assigned
    elsewhere (unassign first). Assigning to the same company is a no-op.
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    _resolve_company(db, body.company_id)
    return _apply_assignment(device, body.company_id, db)


@router.post("/{device_id}/unassign", response_model=DeviceResponse)
async def unassign_device_from_company(
    device_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Reclaim a device from its company (back to inventory). Admin only.

    Clears `company_id`, resets lifecycle to `in_stock`, and issues a fresh
    pairing PIN. Idempotent: unassigning an unassigned device is a no-op.
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if device.company_id is None:
        return device

    device.company_id = None
    device.lifecycle = "in_stock"
    # Fresh PIN so the next client uses a secret the previous owner never saw
    alphabet = string.ascii_uppercase + string.digits
    device.pairing_pin = "".join(secrets.choice(alphabet) for _ in range(6))
    device.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(device)

    logger.info("Device %s (id=%s) unassigned — back to in_stock", device.imei, device.id)
    return device


# ── Endpoint: POST /{device_id}/transfer ──────────────────────────────────────

class DeviceTransferRequest(BaseModel):
    to_company_id: int


class DeviceTransferResponse(BaseModel):
    deviceId: int
    imei: str
    fromCompanyId: Optional[int] = None
    toCompanyId: int
    lifecycle: str

    class Config:
        from_attributes = True


@router.post("/{device_id}/transfer", response_model=DeviceTransferResponse)
async def transfer_device(
    device_id: int,
    body: DeviceTransferRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Transfer a device from one company to another. Global admin only.

    - Device must currently belong to a company (or be unassigned inventory).
    - Target company must exist.
    - Cannot transfer to the same company it's already in.
    - Lifecycle stays 'sold' if already assigned, or becomes 'sold' if unassigned.
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    from app.models.company import Company
    target_company = db.query(Company).filter(Company.id == body.to_company_id).first()
    if not target_company:
        raise HTTPException(status_code=404, detail="Target company not found.")

    from_company_id = device.company_id

    if from_company_id == body.to_company_id:
        raise HTTPException(status_code=400, detail="Device already belongs to this company.")

    try:
        device.company_id = body.to_company_id
        device.lifecycle = "sold"
        device.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(device)

        logger.info(
            "Device %s (id=%d) transferred from company %s to company %d",
            device.imei, device.id, from_company_id, body.to_company_id,
        )

        return DeviceTransferResponse(
            deviceId=device.id,
            imei=device.imei,
            fromCompanyId=from_company_id,
            toCompanyId=body.to_company_id,
            lifecycle=device.lifecycle,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error transferring device: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


@router.post("/{device_id}/verify")
async def verify_device(
    device_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Admin: Manually mark a device as verified and ready to sell.

    This is an optional step after the automatic TCP handshake promotion.
    Use this when you want to explicitly confirm the device is ready for customer pairing
    (e.g. after sending a STATUS# or PARAM# command and confirming the response).

    Transition: registered → in_stock (if device has never connected, force it ready).
    Note: if device is already 'in_stock' or 'sold', this is a no-op.
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if device.lifecycle == 'sold':
        raise HTTPException(
            status_code=409,
            detail="Device is already sold and paired to a customer. Cannot modify lifecycle."
        )

    previous = device.lifecycle
    device.lifecycle = 'in_stock'
    device.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(device)

    return {
        "device_id": device.id,
        "imei": device.imei,
        "previous_lifecycle": previous,
        "lifecycle": device.lifecycle,
        "message": "Device marked as verified and ready to sell."
    }


@router.get("/imei/{imei}/status")
async def get_device_status_by_imei_ownership(
    imei: str,
    db: Session = Depends(get_db),
    clerk_user_id: str = Depends(require_auth),
):
    """
    Get device status by IMEI — company-membership-checked.
    Used by the mobile app's device-wait screen to poll for first signal.
    Returns 404 if the device doesn't exist or isn't accessible.
    """
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    device = db.query(Device).filter(Device.imei == imei).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    require_device_access(device, user, db)
    return {"status": device.status}


@router.get("/{device_id}/status")
async def get_device_status(
    device_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Get device status by numeric device ID or by IMEI.

    Ownership-checked either way: the caller must own the device, or hold an
    ADMIN/SUPER_ADMIN role. Returns 404 if the device doesn't exist or isn't
    accessible to the caller.
    """
    is_imei = device_id.isdigit() and len(device_id) in (15, 16)

    if is_imei:
        device = db.query(Device).filter(Device.imei == device_id).first()
    else:
        try:
            dev_id = int(device_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="device_id must be numeric or a 15-16 digit IMEI")
        device = db.query(Device).filter(Device.id == dev_id).first()

    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user, db)

    if is_imei:
        # Mobile app device-wait screen — minimal payload
        return {"status": device.status}

    return {
        "id": device.id,
        "imei": device.imei,
        "name": device.name,
        "status": device.status,
        "last_update": device.last_update,
        "battery_level": device.battery_level,
        "gsm_signal": device.gsm_signal,
        "location": {
            "latitude": device.last_latitude,
            "longitude": device.last_longitude,
        } if device.last_latitude and device.last_longitude else None,
    }


@router.get("/{device_id}/diagnostics", response_model=DeviceDiagnosticsResponse)
async def get_device_diagnostics(
    device_id: int,
    samples: int = Query(20, ge=2, le=200, description="Number of recent location points to analyze"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Diagnostics for a device, including recent location packet intervals.

    Note: interval stats are based on location packets only (heartbeats are not persisted).
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user, db)

    # Determine last seen and sending status
    last_seen = device.last_update or device.last_connect
    if last_seen:
        seconds_since_last_update = int((datetime.utcnow() - last_seen).total_seconds())
    else:
        seconds_since_last_update = None

    if seconds_since_last_update is None:
        sending_status = "No data"
    elif seconds_since_last_update <= settings.DEVICE_SENDING_STALE_SECONDS:
        sending_status = "Sending"
    elif seconds_since_last_update <= settings.DEVICE_OFFLINE_TIMEOUT_SECONDS:
        sending_status = "Stale"
    else:
        sending_status = "Offline (timed out)"

    # Fetch recent locations for interval analysis
    locations = db.query(Location).filter(
        Location.device_id == device_id
    ).order_by(Location.timestamp.desc()).limit(samples).all()

    last_location_timestamp = locations[0].timestamp if locations else None

    intervals = []
    for i in range(len(locations) - 1):
        dt = locations[i].timestamp - locations[i + 1].timestamp
        intervals.append(abs(dt.total_seconds()))

    if intervals:
        avg_seconds = sum(intervals) / len(intervals)
        min_seconds = min(intervals)
        max_seconds = max(intervals)
        last_interval_seconds = intervals[0]
    else:
        avg_seconds = min_seconds = max_seconds = last_interval_seconds = None

    return DeviceDiagnosticsResponse(
        device_id=device.id,
        imei=device.imei,
        status=device.status,
        last_connect=device.last_connect,
        last_update=device.last_update,
        last_location_timestamp=last_location_timestamp,
        seconds_since_last_update=seconds_since_last_update,
        sending_status=sending_status,
        location_intervals=LocationIntervalStats(
            samples=len(intervals),
            avg_seconds=avg_seconds,
            min_seconds=min_seconds,
            max_seconds=max_seconds,
            last_interval_seconds=last_interval_seconds
        )
    )


# ── Alert settings (per-device push-notification preferences) ─────────────
# app/models/alert_settings.py: master switch, per-alarm-type mute,
# min_push_severity. Read by TCPConnection._send_push_notification
# (app/tcp_server.py) to filter outgoing push notifications — before these
# two endpoints, nothing outside that one call site ever touched this
# table; there was no way for a client to read or write it.

_VALID_MIN_PUSH_SEVERITY = {"low", "medium", "high"}


class AlertSettingsResponse(BaseModel):
    device_id: int
    push_notifications_enabled: bool
    sos_push_enabled: bool
    vibration_push_enabled: bool
    low_battery_push_enabled: bool
    acc_push_enabled: bool
    overspeed_push_enabled: bool
    displacement_push_enabled: bool
    min_push_severity: str


class AlertSettingsUpdate(BaseModel):
    """Partial update — every field optional, only what's provided is
    changed. Same convention as TripSettingsUpdate (app/api/trips.py)."""

    push_notifications_enabled: Optional[bool] = None
    sos_push_enabled: Optional[bool] = None
    vibration_push_enabled: Optional[bool] = None
    low_battery_push_enabled: Optional[bool] = None
    acc_push_enabled: Optional[bool] = None
    overspeed_push_enabled: Optional[bool] = None
    displacement_push_enabled: Optional[bool] = None
    min_push_severity: Optional[str] = None

    @field_validator("min_push_severity")
    @classmethod
    def min_push_severity_valid(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in _VALID_MIN_PUSH_SEVERITY:
            raise ValueError(
                f"min_push_severity must be one of: {', '.join(sorted(_VALID_MIN_PUSH_SEVERITY))}"
            )
        return v


def _alert_settings_response(
    device_id: int, settings_row: Optional[AlertSettings]
) -> AlertSettingsResponse:
    """Row-or-defaults — mirrors AlertSettings' own documented convention
    ("absence of a row means everything enabled")."""
    if settings_row is None:
        return AlertSettingsResponse(
            device_id=device_id,
            push_notifications_enabled=True,
            sos_push_enabled=True,
            vibration_push_enabled=True,
            low_battery_push_enabled=True,
            acc_push_enabled=True,
            overspeed_push_enabled=True,
            displacement_push_enabled=True,
            min_push_severity="low",
        )
    return AlertSettingsResponse(
        device_id=device_id,
        push_notifications_enabled=settings_row.push_notifications_enabled,
        sos_push_enabled=settings_row.sos_push_enabled,
        vibration_push_enabled=settings_row.vibration_push_enabled,
        low_battery_push_enabled=settings_row.low_battery_push_enabled,
        acc_push_enabled=settings_row.acc_push_enabled,
        overspeed_push_enabled=settings_row.overspeed_push_enabled,
        displacement_push_enabled=settings_row.displacement_push_enabled,
        min_push_severity=settings_row.min_push_severity,
    )


@router.get("/{device_id}/alert_settings", response_model=AlertSettingsResponse)
async def get_alert_settings(
    device_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Read this device's push-notification preferences (master switch,
    per-alarm-type mute, min_push_severity)."""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user, db)

    settings_row = db.query(AlertSettings).filter(AlertSettings.device_id == device_id).first()
    return _alert_settings_response(device_id, settings_row)


@router.put("/{device_id}/alert_settings", response_model=AlertSettingsResponse)
async def update_alert_settings(
    device_id: int,
    body: AlertSettingsUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Update this device's push-notification preferences. Creates the row
    (with the documented all-enabled defaults) on first write for a device
    that's never had one — same get-or-create convention as
    TripSettings/get_or_create_trip_settings (app/api/trips.py)."""
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    require_device_access(device, user, db)

    settings_row = db.query(AlertSettings).filter(AlertSettings.device_id == device_id).first()
    if settings_row is None:
        settings_row = AlertSettings(device_id=device_id)
        db.add(settings_row)

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(settings_row, field, value)

    db.commit()
    db.refresh(settings_row)
    return _alert_settings_response(device_id, settings_row)

"""
Onboarding API endpoints — supports the mobile app's 5-step onboarding flow.

Routes:
    POST /api/users              — Step 4: upsert user profile
    POST /api/devices/pair       — Step 5: pair GPS device by IMEI
    GET  /api/devices/{imei}/status — Step 6: poll for first signal
    POST /api/vehicles           — Step 7: register vehicle
    POST /api/payments/initiate  — Step 8 (paid): async mobile money request via
                                    IntouchPay; the payment is only confirmed later
                                    by POST /api/webhooks/intouchpay
    POST /api/subscriptions      — Step 8: activate plan (checks for a
                                    Payment row with status="successful",
                                    however it got there)

All routes require a valid Clerk Bearer token (via require_auth dependency).
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import get_db
from app.core.serialization import UtcDateTime
from app.core.auth import (
    require_auth,
    require_admin,
    get_current_user,
    user_can_access_device,
    require_vehicle_access,
    REQUIRE_ADMIN_ROLES,
)
from app.models.user import User, Role
from app.models.device import Device
from app.models.vehicle import Vehicle
from app.models.subscription import Subscription, Payment
from app.api.devices import _check_pair_rate_limit, _release_device_to_inventory
from app.api.auth import claim_pending_client_user
from app.api.subscriptions import plan_config, plan_purchasable, get_plan_by_slug
from app.services.intouchpay import (
    request_payment as intouch_request_payment,
    get_balance as intouch_get_balance,
    IntouchPayError,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Pricing/limits now come from the admin-configured subscription_plans table
# (see app/api/subscriptions.py plan_config — DB plan wins, legacy values are
# the fallback), so admin edits to a plan immediately affect billing here.


def _get_active_subscription(db: Session, clerk_user_id: str) -> Optional[Subscription]:
    """The subscription that is genuinely active right now: status=="active"
    AND not yet past expires_at.

    Checked inline rather than trusting the cached `status` column alone,
    because scripts/cron_expiry.py only flips a lapsed subscription's status
    to "expired" every 15 minutes. Without the expires_at check here, a
    request landing in that window would see a stale "active" row and either
    grant a vehicle slot it shouldn't, or let a renewal/upgrade silently
    no-op against an already-lapsed subscription instead of recording the
    new one. The cron still owns the async side (expiry-warning and
    expired-notification emails/pushes) — this only closes the request-time
    gap for the enforcement points that gate/short-circuit on "is there a
    subscription actively covering this account right now."
    """
    return (
        db.query(Subscription)
        .filter(
            Subscription.clerk_user_id == clerk_user_id,
            Subscription.status == "active",
            Subscription.expires_at > datetime.utcnow(),
        )
        .first()
    )


def _cancel_active_subscription(db: Session, clerk_user_id: str) -> Optional[Subscription]:
    """Shared cancellation logic — the one code path behind both the
    self-service (POST /api/subscriptions/cancel) and admin
    (POST /api/admin/subscriptions/{user_id}/cancel) cancel endpoints, so
    "what cancelling actually does" is defined in exactly one place. Mutates
    and returns the subscription (caller commits); returns None if there is
    nothing genuinely active to cancel.

    Immediate, not "at period end": this product has no auto-renewal at all
    (subscriptions lapse on their own at expires_at; renewing is always an
    explicit re-purchase, see create_subscription/upgrade_subscription), so
    "cancel to stop being charged again" doesn't apply the way it would for
    a recurring-billing SaaS. The only two readings left are "stop access
    now" (this) or "let access continue until expires_at but flag as
    cancelled" (deferred) — and the deferred version isn't free: it would
    need a distinct status enforcement still honors until expires_at (e.g.
    "cancelling"), because right now the ONLY thing that decides plan
    entitlement is `_get_active_subscription`'s status=="active" check —
    flipping status to "cancelled" immediately drops the account to trial
    limits right away regardless of intent. Implementing that properly is a
    real product/billing decision (does a mid-period prepaid cancellation
    imply a refund? does postpaid still get invoiced for time used?) that's
    out of scope for this pass — flagging it rather than silently picking a
    semantics that assumes an answer.
    """
    sub = _get_active_subscription(db, clerk_user_id)
    if not sub:
        return None
    sub.status = "cancelled"
    sub.updated_at = datetime.utcnow()
    return sub


# ── Schemas ───────────────────────────────────────────────────────────────────

class UserCreateRequest(BaseModel):
    firstName: str
    lastName:  str
    email:     str
    role:      Optional[str] = "owner"


class UserResponse(BaseModel):
    userId: int
    alreadyExists: bool

    class Config:
        from_attributes = True


class DevicePairRequest(BaseModel):
    imei: str
    pairingPin: Optional[str] = None  # Required for whitelisted devices


class DeviceStatusResponse(BaseModel):
    status: str   # pending | online | offline


class VehicleCreateRequest(BaseModel):
    nickname:   str
    plate:      str
    make:       str
    model:      str
    deviceImei: str


class VehicleResponse(BaseModel):
    vehicleId: int


class VehicleUpdateRequest(BaseModel):
    nickname: str


class PaymentInitiateRequest(BaseModel):
    planId: str
    phone:  str  # mobile money number, e.g. "250781234567"


class PaymentInitiateResponse(BaseModel):
    txRef:  str
    status: str   # "pending" | "failed"
    message: Optional[str] = None


class SubscriptionRequest(BaseModel):
    planId: str


class SubscriptionResponse(BaseModel):
    subscriptionId: int
    expiresAt:      UtcDateTime


class SubscriptionUpgradeRequest(BaseModel):
    planId: str
    txRef: str


class SubscriptionCancelResponse(BaseModel):
    status: str  # always "cancelled" on success
    expiresAt: Optional[UtcDateTime] = None


class PaymentRecord(BaseModel):
    txRef: str
    planId: str
    amount: float
    status: str
    createdAt: UtcDateTime

    class Config:
        from_attributes = True


class BillingResponse(BaseModel):
    currentPlan: str
    expiresAt: Optional[UtcDateTime]
    payments: list[PaymentRecord]


# ── Endpoint 1: POST /api/users  (Step 4) ────────────────────────────────────

@router.post("/users", response_model=UserResponse, status_code=201)
async def create_or_update_user(
    body: UserCreateRequest,
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """
    Upsert user profile after OTP verification.
    Idempotent — safe to call multiple times (returns existing user without error).
    """
    if not body.firstName or not body.lastName or not body.email:
        raise HTTPException(status_code=400, detail="firstName, lastName, and email are required")

    try:
        existing = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()

        # If the client was pre-provisioned via an admin Clerk invitation, adopt
        # the pending row here (rather than creating a duplicate) — keeping any
        # device assignment the admin already made.
        if existing is None:
            existing = claim_pending_client_user(
                db,
                clerk_user_id=clerk_user_id,
                email=body.email,
                first_name=body.firstName,
                last_name=body.lastName,
            )

        if existing:
            existing.first_name = body.firstName
            existing.last_name  = body.lastName
            existing.email      = body.email
            existing.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(existing)
            logger.info("Updated user profile: %s", clerk_user_id)
            return UserResponse(userId=existing.id, alreadyExists=True)

        # First user gets SUPER_ADMIN, rest default to USER
        is_first = db.query(User).count() == 0
        initial_role = Role.SUPER_ADMIN if is_first else Role.USER

        user = User(
            clerk_user_id=clerk_user_id,
            first_name=body.firstName,
            last_name=body.lastName,
            email=body.email,
            role=initial_role,
            onboarding_step=4,
            onboarding_complete=False,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("Created user profile: %s (id=%d)", clerk_user_id, user.id)
        return UserResponse(userId=user.id, alreadyExists=False)

    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error creating user: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


# ── Endpoint 2: POST /api/devices/pair  (Step 5) ─────────────────────────────

@router.post("/devices/pair")
async def pair_device(
    body: DevicePairRequest,
    request: Request,
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """
    Pair an existing GPS device to the authenticated user's account.

    The IMEI must already exist in the `devices` table (pre-loaded by admin).
    Returns 404 if the IMEI is unknown, 409 if already paired to another user.
    """
    # Rate limit: max 5 attempts per IP per 60 seconds
    client_ip = request.client.host if request.client else "unknown"
    _check_pair_rate_limit(client_ip)

    imei = body.imei.strip()
    if not imei.isdigit() or len(imei) not in (15, 16):
        raise HTTPException(status_code=400, detail="Invalid IMEI — must be exactly 15 or 16 digits")

    # Look up by IMEI
    device = db.query(Device).filter(Device.imei == imei).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found. Check your IMEI.")

    # Resolve user record
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found. Complete profile step first.")

    # Conflict: device already claimed by a different user
    if device.user_id and device.user_id != user.id:
        raise HTTPException(
            status_code=409,
            detail="This device is already registered to another account. Contact support if you believe this is an error.",
        )

    # Guard: device must have proven it's functional (lifecycle != 'registered')
    # 'registered' means SIM was inserted but device never connected via TCP.
    if device.lifecycle == 'registered':
        raise HTTPException(
            status_code=409,
            detail="This device hasn't connected to our servers yet. Please power it on and ensure it has network coverage.",
        )

    # PIN validation: if device has a pairing_pin set, the client must supply the correct one
    if device.pairing_pin:
        if not body.pairingPin:
            raise HTTPException(
                status_code=403,
                detail="This device requires a Pairing PIN. Check the card inside the device box."
            )
        if body.pairingPin.strip().upper() != device.pairing_pin.strip().upper():
            raise HTTPException(
                status_code=403,
                detail="Incorrect Pairing PIN. Please check the card inside the device box."
            )

    try:
        device.user_id    = user.id
        device.lifecycle  = 'sold'   # Ownership transferred to customer
        device.updated_at = datetime.utcnow()

        user.onboarding_step = 5
        user.updated_at = datetime.utcnow()

        db.commit()
        db.refresh(device)

        logger.info("Paired device IMEI=%s to user %s (lifecycle=sold)", imei, clerk_user_id)
        return {"deviceId": device.id, "status": device.status, "imei": device.imei, "lifecycle": device.lifecycle}

    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error pairing device: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


# ── Endpoint 3: GET /api/devices/{imei}/status  (Step 6) ──────────────────────

@router.get("/devices/{imei}/status", response_model=DeviceStatusResponse)
async def get_device_status_by_imei(
    imei: str,
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """
    Return current status of a device identified by IMEI.
    Only returns devices belonging to the authenticated user.

    Polled every 5 s by device-wait.tsx — status transitions:
        pending  →  (first GPS packet arrives via TCP)  →  online
    """
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    device = db.query(Device).filter(
        Device.imei == imei,
        Device.user_id == user.id,
    ).first()

    if not device:
        raise HTTPException(status_code=404, detail="Device not found or not paired to your account")

    return DeviceStatusResponse(status=device.status)


# ── Endpoint 4: POST /api/vehicles  (Step 7) ──────────────────────────────────

@router.post("/vehicles", response_model=VehicleResponse, status_code=201)
async def create_vehicle(
    body: VehicleCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Register a vehicle and link it to a paired GPS device.

    Ownership is checked with the same user_can_access_device helper
    device routes use (RBAC fix): the device must be paired to *someone*,
    and the caller must either be that owner or hold an admin role. This
    lets an admin register a vehicle on a client's behalf — the vehicle
    (and plan/vehicle-limit accounting) is then attributed to the device's
    actual owner, not the admin.
    """
    for field, value in [("nickname", body.nickname), ("plate", body.plate),
                         ("make", body.make), ("model", body.model)]:
        if not value or not value.strip():
            raise HTTPException(status_code=400, detail=f"{field} is required")

    device = db.query(Device).filter(Device.imei == body.deviceImei).first()
    if device is None or device.user_id is None or not user_can_access_device(user, device):
        raise HTTPException(status_code=403, detail="Device not paired to your account")

    owner = device.user
    owner_clerk_id = owner.clerk_user_id

    # Enforce plan limits (admin-configured via subscription_plans) —
    # always against the device owner's plan/vehicle count, not the
    # caller's, so an admin acting on a client's behalf doesn't get
    # measured against their own (nonexistent) subscription. Checked
    # expiry-freshness inline (_get_active_subscription), not just the
    # cached status column — see its docstring for why.
    sub = _get_active_subscription(db, owner_clerk_id)

    # sub.plan_id is a real FK (migrations 041/042) — plan_config/the error
    # message below both want the plan's slug, not its raw integer id.
    current_plan = sub.plan.slug if sub and sub.plan else "trial"
    vehicle_limit = plan_config(db, current_plan)["max_devices"]

    if vehicle_limit is not None:
        current_vehicles_count = db.query(Vehicle).filter(Vehicle.clerk_user_id == owner_clerk_id).count()
        if current_vehicles_count >= vehicle_limit:
            raise HTTPException(
                status_code=403,
                detail=f"Your {current_plan} plan only allows up to {vehicle_limit} vehicle(s). Please upgrade your plan."
            )

    try:
        vehicle = Vehicle(
            clerk_user_id=owner_clerk_id,
            device_id=device.id,
            nickname=body.nickname.strip(),
            plate=body.plate.strip().upper(),
            make=body.make.strip(),
            model=body.model.strip(),
        )
        db.add(vehicle)

        owner.onboarding_step = 7
        owner.updated_at = datetime.utcnow()

        db.commit()
        db.refresh(vehicle)

        logger.info("Vehicle created id=%d for user %s (by %s)", vehicle.id, owner_clerk_id, user.clerk_user_id)
        return VehicleResponse(vehicleId=vehicle.id)

    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error creating vehicle: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


# ── Charging helpers (per-device plans / postpaid plans) ─────────────────────

def _device_count_for_user(db: Session, clerk_user_id: str) -> int:
    """Number of GPS devices paired to the user's account (min 1 — buying a
    plan always covers at least one vehicle)."""
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        return 1
    count = db.query(Device).filter(Device.user_id == user.id).count()
    return max(1, count)


def _effective_charge(db: Session, cfg: dict, clerk_user_id: str) -> float:
    """Price actually charged to this user for a plan.

    - per_device plans: price × the user's paired device count (capped by the
      plan's max_devices when set — "Up to N vehicles")
    - flat plans: price once, regardless of device count

    The same formula runs at payment initiation and at subscription
    activation, so the amount collected always matches the amount stored on
    the Subscription row."""
    price = float(cfg.get("price", 0.0))
    if cfg.get("charge_scope") == "per_device":
        devices = _device_count_for_user(db, clerk_user_id)
        max_devices = cfg.get("max_devices")
        if max_devices and devices > max_devices:
            devices = max_devices
        price = round(price * devices, 2)
    return price


# ── Endpoint 5: POST /api/payments/initiate  (Step 8, paid plans, IntouchPay) ──

@router.post("/payments/initiate", response_model=PaymentInitiateResponse)
async def initiate_payment(
    body: PaymentInitiateRequest,
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """
    Start an IntouchPay mobile money collection.

    This does NOT confirm payment — it only dispatches the USSD approval
    prompt to the customer's phone and returns immediately. The Payment row
    is created with status="pending" and
    is only ever moved to "successful" by POST /api/webhooks/intouchpay (or,
    if that never arrives, by the cron reconciliation job in
    scripts/cron_expiry.py). The mobile app must poll/retry
    POST /api/subscriptions afterwards — that endpoint already requires a
    Payment with status="successful" for paid plans and is unchanged.
    """
    if not plan_purchasable(db, body.planId):
        raise HTTPException(
            status_code=400,
            detail="Invalid or unavailable planId — choose an active plan from the pricing screen.",
        )

    # Payment.plan_id is a real FK (migrations 041/042) — a subscription_plans
    # row must exist to write this Payment, not just a FALLBACK_PLANS entry.
    # plan_purchasable's fallback-slug-with-no-DB-row branch is unreachable in
    # practice once migration 041 has seeded the three legacy slugs; this is
    # a defensive 500 (not a 400) precisely because it should never happen —
    # a missing row here means those migrations haven't been run.
    plan = get_plan_by_slug(db, body.planId, include_inactive=True)
    if plan is None:
        logger.error("No subscription_plans row for purchasable planId=%s — has migration 041 run?", body.planId)
        raise HTTPException(status_code=500, detail="This plan isn't fully set up yet. Please try again later.")

    phone = body.phone.strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone is required")

    cfg = plan_config(db, body.planId)

    # Postpaid plans are invoiced AFTER the period — there is nothing to
    # collect up-front, so reject the mobile-money flow rather than charging
    # the customer the wrong amount.
    if cfg.get("billing_model") == "postpaid":
        raise HTTPException(
            status_code=400,
            detail="This plan is postpaid — you will be invoiced after the period; no upfront payment is required.",
        )

    # per_device plans are charged price × the user's device count; flat
    # plans are charged the price once.
    amount = _effective_charge(db, cfg, clerk_user_id)

    # Our own reference — IntouchPay requires this to be globally unique
    # across every request ever sent to them.
    tx_ref = f"IP{uuid.uuid4().hex}"

    try:
        resp = await intouch_request_payment(
            amount=amount,
            phone=phone,
            transaction_id=tx_ref,
        )
    except IntouchPayError as exc:
        logger.error("IntouchPay requestpayment error for planId=%s user=%s: %s", body.planId, clerk_user_id, exc)
        raise HTTPException(status_code=502, detail="Could not reach IntouchPay. Retry later.")

    accepted = bool(resp.get("success")) and resp.get("responsecode") == "1000"
    status = "pending" if accepted else "failed"

    try:
        payment = Payment(
            clerk_user_id=clerk_user_id,
            tx_ref=tx_ref,
            plan_id=plan.id,
            amount=amount,
            currency=cfg["currency"],
            status=status,
            # Not yet "verified" — this timestamp marks when the request was
            # initiated. It's overwritten with the real confirmation time once
            # the webhook (or cron reconciliation) confirms success. Reusing
            # this column avoids a schema change; Payment has no separate
            # created_at field and GET /api/billing maps verified_at -> createdAt.
            verified_at=datetime.utcnow(),
        )
        db.add(payment)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error saving pending payment tx_ref=%s: %s", tx_ref, exc)
        raise HTTPException(status_code=500, detail="Database error")

    if not accepted:
        logger.warning("IntouchPay requestpayment rejected for tx_ref=%s: %s", tx_ref, resp)
        return PaymentInitiateResponse(
            txRef=tx_ref, status="failed",
            message=resp.get("message") or "Payment request was rejected.",
        )

    logger.info("IntouchPay payment initiated: tx_ref=%s planId=%s user=%s", tx_ref, body.planId, clerk_user_id)
    return PaymentInitiateResponse(
        txRef=tx_ref, status="pending",
        message=resp.get("message") or "Approve the payment on your phone to continue.",
    )


# ── Endpoint 6: POST /api/subscriptions  (Step 8) ─────────────────────────────

@router.post("/subscriptions", response_model=SubscriptionResponse, status_code=201)
async def create_subscription(
    body: SubscriptionRequest,
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """
    Activate a subscription plan for the authenticated user.

    - For "trial": enforces one trial per user lifetime
    - For paid plans: assumes /api/payments/verify was called successfully beforehand
    - Marks onboarding_step=9 and onboarding_complete=true on the user record
    """
    if not plan_purchasable(db, body.planId):
        raise HTTPException(
            status_code=400,
            detail="Invalid or unavailable planId — choose an active plan from the pricing screen.",
        )

    # Subscription.plan_id is a real FK (migrations 041/042) — see
    # initiate_payment's identical check/comment for why this 500 should
    # never actually fire in practice.
    plan = get_plan_by_slug(db, body.planId, include_inactive=True)
    if plan is None:
        logger.error("No subscription_plans row for purchasable planId=%s — has migration 041 run?", body.planId)
        raise HTTPException(status_code=500, detail="This plan isn't fully set up yet. Please try again later.")

    # Check if a genuinely active subscription already exists (expiry-aware,
    # not just the cached status column — see _get_active_subscription's
    # docstring). Without the expiry check, a customer renewing right after
    # their subscription lapsed — but before the next cron_expiry.py tick
    # flips status to "expired" — would hit this branch and get silently
    # short-circuited back to their already-expired subscription instead of
    # the new one they just paid for.
    existing_sub = _get_active_subscription(db, clerk_user_id)

    if existing_sub:
        # Subscription already exists — do NOT reject.
        # Just ensure the user record reflects onboarding_complete=True
        # (it may have been missed if the app crashed after payment).
        try:
            user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
            if user and not user.onboarding_complete:
                user.onboarding_step     = 9
                user.onboarding_complete = True
                user.updated_at          = datetime.utcnow()
                db.commit()
                logger.info("Healed onboarding_complete for user %s (sub already existed)", clerk_user_id)
        except SQLAlchemyError as exc:
            db.rollback()
            logger.warning("Could not heal onboarding_complete: %s", exc)

        logger.info("Subscription already active for user %s — returning existing", clerk_user_id)
        return SubscriptionResponse(subscriptionId=existing_sub.id, expiresAt=existing_sub.expires_at)

    # For trial: also check historical (expired/cancelled) subscriptions to prevent re-use
    if body.planId == "trial":
        ever_used = db.query(Subscription).filter(
            Subscription.clerk_user_id == clerk_user_id
        ).first()
        if ever_used:
            raise HTTPException(status_code=409, detail="Free trial already used. Please choose a paid plan.")

    cfg = plan_config(db, body.planId)
    is_postpaid = cfg.get("billing_model") == "postpaid"
    charge = _effective_charge(db, cfg, clerk_user_id)

    # For paid prepaid plans: enforce that a successful payment was made
    # before activating the subscription. The mobile app should call
    # /api/payments/verify first, but this backend check prevents a rogue
    # or buggy client from skipping payment entirely.
    #
    # Postpaid plans are the exception: they are invoiced AFTER the period,
    # so activation is flagged here (logged, no Payment row required)
    # instead of rejected.
    if body.planId != "trial" and not is_postpaid:
        payment = db.query(Payment).filter(
            Payment.clerk_user_id == clerk_user_id,
            Payment.plan_id == plan.id,
            Payment.status == "successful",
        ).first()
        if not payment:
            raise HTTPException(
                status_code=402,
                detail="Payment required. Complete payment verification before activating a paid plan.",
            )

    expires_at = datetime.utcnow() + timedelta(days=cfg["days"])

    try:
        subscription = Subscription(
            clerk_user_id=clerk_user_id,
            plan_id=plan.id,
            status="active",
            price=charge,
            started_at=datetime.utcnow(),
            expires_at=expires_at,
        )
        db.add(subscription)

        # Finalise onboarding on the user record
        user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
        if user:
            user.onboarding_step     = 9
            user.onboarding_complete = True
            user.updated_at          = datetime.utcnow()

        db.commit()
        db.refresh(subscription)

        logger.info(
            "Subscription activated: planId=%s price=%.2f%s user=%s expires=%s",
            body.planId, charge,
            " (postpaid — to be invoiced)" if is_postpaid else "",
            clerk_user_id, expires_at.isoformat(),
        )
        return SubscriptionResponse(subscriptionId=subscription.id, expiresAt=expires_at)

    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error creating subscription: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


# ── Endpoint 7: POST /api/subscriptions/upgrade  ──────────────────────────────

@router.post("/subscriptions/upgrade", response_model=SubscriptionResponse, status_code=201)
async def upgrade_subscription(
    body: SubscriptionUpgradeRequest,
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """
    Upgrade an existing subscription to a higher tier.
    Assumes /api/payments/verify was called successfully beforehand.
    """
    if not plan_purchasable(db, body.planId):
        raise HTTPException(
            status_code=400,
            detail="Invalid or unavailable planId — choose an active plan from the pricing screen.",
        )

    # Subscription.plan_id is a real FK (migrations 041/042) — see
    # initiate_payment's identical check/comment for why this 500 should
    # never actually fire in practice.
    plan = get_plan_by_slug(db, body.planId, include_inactive=True)
    if plan is None:
        logger.error("No subscription_plans row for purchasable planId=%s — has migration 041 run?", body.planId)
        raise HTTPException(status_code=500, detail="This plan isn't fully set up yet. Please try again later.")

    cfg = plan_config(db, body.planId)
    is_postpaid = cfg.get("billing_model") == "postpaid"

    # 1. Confirm payment record exists and is successful — skipped for
    #    postpaid target plans (invoiced after the period; there is no
    #    upfront payment to verify).
    if not is_postpaid:
        payment = db.query(Payment).filter(
            Payment.tx_ref == body.txRef,
            Payment.clerk_user_id == clerk_user_id,
            Payment.status == "successful"
        ).first()

        if not payment:
            raise HTTPException(status_code=402, detail="Payment not verified or not found")

    # 2. Confirm upgrade (different plan slug — the mobile app controls
    # pricing-tier ordering, the backend just ensures they're not re-buying
    # the same plan. Comparing live prices is fragile because admin edits
    # to a plan would instantly change the comparison result for existing
    # subscribers, unexpectedly blocking or allowing upgrades.)
    #
    # Expiry-aware (_get_active_subscription), not just the cached status
    # column: otherwise a customer renewing the SAME plan right after it
    # lapsed — but before cron_expiry.py's next tick — would be wrongly
    # blocked with "Already on this plan" instead of being allowed to
    # re-subscribe. A stale "active" row that's actually past expires_at
    # just won't match here; it's left for the cron to flip to "expired"
    # (harmless — nothing treats it as authoritative once its own
    # expires_at has passed, see resolve_owner_plan/_get_active_subscription).
    current_sub = _get_active_subscription(db, clerk_user_id)

    if current_sub and current_sub.plan_id == plan.id:
        raise HTTPException(status_code=400, detail="Already on this plan. Choose a different plan to upgrade.")

    # 3. Cancel current, create new
    try:
        if current_sub:
            current_sub.status = "cancelled"
            current_sub.updated_at = datetime.utcnow()

        charge = _effective_charge(db, cfg, clerk_user_id)
        expires_at = datetime.utcnow() + timedelta(days=cfg["days"])

        new_sub = Subscription(
            clerk_user_id=clerk_user_id,
            plan_id=plan.id,
            status="active",
            price=charge,
            started_at=datetime.utcnow(),
            expires_at=expires_at,
        )
        db.add(new_sub)

        # Update user
        user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
        if user:
            user.onboarding_step = 9
            user.onboarding_complete = True
            user.updated_at = datetime.utcnow()

        db.commit()
        db.refresh(new_sub)

        logger.info(
            "Subscription upgraded: planId=%s price=%.2f%s user=%s expires=%s",
            body.planId, charge,
            " (postpaid — to be invoiced)" if is_postpaid else "",
            clerk_user_id, expires_at.isoformat(),
        )
        return SubscriptionResponse(subscriptionId=new_sub.id, expiresAt=expires_at)

    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error upgrading subscription: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


# ── Endpoint 8: POST /api/subscriptions/cancel ────────────────────────────────

@router.post("/subscriptions/cancel", response_model=SubscriptionCancelResponse)
async def cancel_subscription(
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """
    Self-service cancellation of the caller's own subscription. Immediate —
    see _cancel_active_subscription's docstring for why "at period end" was
    not implemented in this pass, and what would be needed to add it later.
    404s if there is nothing genuinely active to cancel (already
    expired/cancelled, or never subscribed).
    """
    sub = _cancel_active_subscription(db, clerk_user_id)
    if not sub:
        raise HTTPException(status_code=404, detail="No active subscription to cancel")

    try:
        db.commit()
        db.refresh(sub)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error cancelling subscription: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")

    logger.info("Subscription %s cancelled by user %s", sub.id, clerk_user_id)
    return SubscriptionCancelResponse(status="cancelled", expiresAt=sub.expires_at)


# ── Endpoint 9: GET /api/billing ──────────────────────────────────────────────

@router.get("/billing", response_model=BillingResponse)
async def get_billing_history(
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """
    Get current active plan and payment history for the user.
    """
    sub = (
        db.query(Subscription)
        .options(joinedload(Subscription.plan))
        .filter(
            Subscription.clerk_user_id == clerk_user_id,
            Subscription.status == "active"
        )
        .first()
    )

    payments = (
        db.query(Payment)
        .options(joinedload(Payment.plan))
        .filter(Payment.clerk_user_id == clerk_user_id)
        .order_by(Payment.verified_at.desc())
        .limit(20)
        .all()
    )

    # currentPlan/planId are the plan's *slug* — the mobile app's wire
    # contract predates Subscription.plan_id/Payment.plan_id becoming real
    # FKs (migrations 041/042), so this always resolves through the
    # relationship now rather than reading the (now-integer) column directly.
    payment_records = []
    for p in payments:
        payment_records.append(PaymentRecord(
            txRef=p.tx_ref,
            planId=p.plan.slug if p.plan else "",
            amount=p.amount,
            status=p.status,
            createdAt=p.verified_at
        ))

    return BillingResponse(
        currentPlan=sub.plan.slug if sub and sub.plan else "trial",
        expiresAt=sub.expires_at if sub else None,
        payments=payment_records
    )


# ── Endpoint 10: GET /api/billing/admin/summary ──────────────────────────────

class AdminPaymentSummary(BaseModel):
    clerk_user_id: str
    total_payments: int
    total_paid_amount: float
    currency: str


@router.get("/billing/admin/summary", response_model=list[AdminPaymentSummary])
async def get_admin_billing_summary(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Per-client payment totals (successful payments only) for the admin
    billing dashboard. Grouped by currency so multi-currency accounts are not
    silently summed together."""
    rows = (
        db.query(
            Payment.clerk_user_id,
            func.count(Payment.id),
            func.coalesce(func.sum(Payment.amount), 0.0),
            Payment.currency,
        )
        .filter(Payment.status == "successful")
        .group_by(Payment.clerk_user_id, Payment.currency)
        .order_by(Payment.clerk_user_id.asc())
        .all()
    )
    return [
        AdminPaymentSummary(
            clerk_user_id=r[0],
            total_payments=r[1],
            total_paid_amount=float(r[2]),
            currency=r[3] or "RWF",
        )
        for r in rows
    ]


# ── Endpoint 10b: GET /api/billing/admin/intouchpay-balance ───────────────────
# Distinct from AdminPaymentSummary above: that's what clients owe/have paid
# *us*; this is the money actually sitting in *our* IntouchPay merchant
# wallet — the number that matters before any future disbursement feature
# (there is none yet — see app/services/intouchpay.py's get_balance, added
# without a matching requestdeposit/disbursement flow) is ever built.

class IntouchBalanceResponse(BaseModel):
    balance: Optional[float] = None
    currency: Optional[str] = None
    # Raw IntouchPay status fields, surfaced as-is rather than re-interpreted
    # — an auth failure (e.g. responsecode 0005) comes back as an ordinary
    # dict with no "balance" key, not a raised exception (see get_balance's
    # docstring), so the caller needs these to tell "checked, zero" apart
    # from "the check itself failed".
    status: Optional[str] = None
    responsecode: Optional[str] = None
    message: Optional[str] = None


@router.get("/billing/admin/intouchpay-balance", response_model=IntouchBalanceResponse)
async def get_admin_intouchpay_balance(
    _: User = Depends(require_admin),
):
    """The current IntouchPay merchant account balance — admin only."""
    try:
        data = await intouch_get_balance()
    except IntouchPayError as exc:
        logger.error("IntouchPay getbalance error: %s", exc)
        raise HTTPException(status_code=502, detail="Could not reach IntouchPay. Retry later.")

    balance = data.get("balance")
    return IntouchBalanceResponse(
        balance=float(balance) if balance is not None else None,
        currency=data.get("currency"),
        status=data.get("status"),
        responsecode=data.get("responsecode"),
        message=data.get("message"),
    )


# ── Endpoint 11: Admin per-user subscription management ──────────────────────
# Phase 2 of the plan/subscription consolidation (see
# app/services/plan_resolution.py for Phase 1): lets an admin view, assign,
# extend, or cancel a user's actual Subscription — the one row
# create_vehicle's vehicle-limit check reads — instead of the retired
# device-level plan link. car-management-portal's per-user subscription
# screen calls these.

class AdminSubscriptionResponse(BaseModel):
    subscription_id: Optional[int] = None
    user_id: int
    clerk_user_id: str
    plan_id: Optional[str] = None    # slug
    plan_name: Optional[str] = None
    status: str                       # active | expired | cancelled | none
    price: Optional[float] = None
    started_at: Optional[UtcDateTime] = None
    expires_at: Optional[UtcDateTime] = None


class AdminAssignPlanRequest(BaseModel):
    plan_id: str                              # slug — required
    expires_at: Optional[UtcDateTime] = None  # override the computed expiry
    price: Optional[float] = None             # override the computed/snapshot price (e.g. 0 for a comp)


class AdminExtendExpiryRequest(BaseModel):
    expires_at: UtcDateTime


def _get_user_or_404(db: Session, user_id: int) -> User:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _admin_subscription_response(
    user: User, sub: Optional[Subscription], db: Session
) -> AdminSubscriptionResponse:
    # AdminSubscriptionResponse.plan_id is the plan's *slug* (see its field
    # comment) — resolved via the relationship now that Subscription.plan_id
    # itself is a real integer FK (migrations 041/042).
    plan = sub.plan if sub else None

    if sub is None:
        status = "none"
    elif sub.status == "cancelled":
        status = "cancelled"
    elif sub.status == "active" and sub.expires_at and sub.expires_at > datetime.utcnow():
        status = "active"
    else:
        status = "expired"

    return AdminSubscriptionResponse(
        subscription_id=sub.id if sub else None,
        user_id=user.id,
        clerk_user_id=user.clerk_user_id,
        plan_id=plan.slug if plan else None,
        plan_name=plan.name if plan else None,
        status=status,
        price=sub.price if sub else None,
        started_at=sub.started_at if sub else None,
        expires_at=sub.expires_at if sub else None,
    )


@router.get("/admin/subscriptions/{user_id}", response_model=AdminSubscriptionResponse)
async def get_admin_user_subscription(
    user_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """The user's actual, currently-enforced subscription (most recent row,
    any status) — the same one create_vehicle's vehicle-limit check reads."""
    user = _get_user_or_404(db, user_id)
    sub = (
        db.query(Subscription)
        .options(joinedload(Subscription.plan))
        .filter(Subscription.clerk_user_id == user.clerk_user_id)
        .order_by(Subscription.created_at.desc())
        .first()
    )
    return _admin_subscription_response(user, sub, db)


@router.put("/admin/subscriptions/{user_id}", response_model=AdminSubscriptionResponse)
async def admin_assign_subscription(
    user_id: int,
    body: AdminAssignPlanRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """
    Directly assign a plan to a user, bypassing payment — an admin override
    (comp, manual correction, migrating a legacy client onto the new plan
    model, etc.). Cancels any existing active subscription and creates a new
    one, mirroring upgrade_subscription's cancel-then-create semantics, but
    with no payment check and admin-controlled price/expiry. Any existing
    plan may be targeted, active or deactivated — an admin overriding a
    subscription is assumed to know what they're doing (e.g. grandfathering
    someone back onto a retired scheme).
    """
    user = _get_user_or_404(db, user_id)
    plan = get_plan_by_slug(db, body.plan_id, include_inactive=True)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    cfg = plan_config(db, body.plan_id)
    try:
        current_sub = db.query(Subscription).filter(
            Subscription.clerk_user_id == user.clerk_user_id,
            Subscription.status == "active",
        ).first()
        if current_sub:
            current_sub.status = "cancelled"
            current_sub.updated_at = datetime.utcnow()

        expires_at = body.expires_at or (datetime.utcnow() + timedelta(days=cfg["days"]))
        price = body.price if body.price is not None else cfg.get("price", 0.0)

        new_sub = Subscription(
            clerk_user_id=user.clerk_user_id,
            plan_id=plan.id,
            status="active",
            price=price,
            started_at=datetime.utcnow(),
            expires_at=expires_at,
        )
        db.add(new_sub)
        db.commit()
        db.refresh(new_sub)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error assigning subscription: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")

    logger.info(
        "Admin %s assigned plan %s to user %s (id=%s), expires=%s, price=%.2f",
        admin.clerk_user_id, plan.slug, user.clerk_user_id, user.id,
        expires_at.isoformat(), price,
    )
    return _admin_subscription_response(user, new_sub, db)


@router.patch("/admin/subscriptions/{user_id}/expiry", response_model=AdminSubscriptionResponse)
async def admin_extend_subscription(
    user_id: int,
    body: AdminExtendExpiryRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """
    Manually adjust the expiry of a user's current subscription (extend a
    grace period, correct a mistake) without touching its plan or price.
    Requires an existing subscription row (any status) to adjust — use
    PUT /admin/subscriptions/{user_id} to create one from scratch.

    Pushing expires_at into the future re-activates a subscription that had
    lapsed to "expired" (that's the point of extending it); an explicit
    "cancelled" status is left alone — undoing a cancellation is a deliberate
    action, not a side effect of nudging a date, so use the assign-plan
    endpoint to actively re-subscribe a cancelled user instead.
    """
    user = _get_user_or_404(db, user_id)
    sub = (
        db.query(Subscription)
        .filter(Subscription.clerk_user_id == user.clerk_user_id)
        .order_by(Subscription.created_at.desc())
        .first()
    )
    if not sub:
        raise HTTPException(status_code=404, detail="User has no subscription to adjust")

    sub.expires_at = body.expires_at
    if sub.status == "expired" and body.expires_at > datetime.utcnow():
        sub.status = "active"
    sub.updated_at = datetime.utcnow()
    try:
        db.commit()
        db.refresh(sub)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error extending subscription expiry: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")

    logger.info(
        "Admin %s adjusted expiry for user %s (id=%s) to %s",
        admin.clerk_user_id, user.clerk_user_id, user.id, body.expires_at.isoformat(),
    )
    return _admin_subscription_response(user, sub, db)


@router.post("/admin/subscriptions/{user_id}/cancel", response_model=AdminSubscriptionResponse)
async def admin_cancel_subscription(
    user_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """
    Cancel a user's active subscription, admin-triggered. Shares
    _cancel_active_subscription with the self-service
    POST /api/subscriptions/cancel endpoint — one cancellation code path,
    two auth-gated entry points (self vs. admin-on-behalf-of-a-user) — so
    both are immediate for the same reason (see that function's docstring:
    this product has no auto-renewal, so "at period end" isn't a free
    change, it needs a status enforcement still honors until expiry).
    """
    user = _get_user_or_404(db, user_id)
    sub = _cancel_active_subscription(db, user.clerk_user_id)
    if not sub:
        raise HTTPException(status_code=404, detail="User has no active subscription to cancel")

    try:
        db.commit()
        db.refresh(sub)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error cancelling subscription: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")

    logger.info(
        "Admin %s cancelled subscription for user %s (id=%s)",
        admin.clerk_user_id, user.clerk_user_id, user.id,
    )
    return _admin_subscription_response(user, sub, db)


# ── Vehicle serialization (shared by list + update) ───────────────────────────

def _serialize_vehicle(v: Vehicle, owner: Optional[User] = None) -> dict:
    device = v.device
    return {
        "id":        v.id,
        "nickname":  v.nickname,
        "plate":     v.plate,
        "make":      v.make,
        "model":     v.model,
        "device": {
            "id":        device.id      if device else None,
            "imei":      device.imei    if device else None,
            "status":    device.status  if device else "unknown",
            "latitude":  device.last_latitude  if device else None,
            "longitude": device.last_longitude if device else None,
            "last_seen": device.last_update    if device else None,
        } if device else None,
        "created_at": v.created_at,
        # The vehicle's real owner — None for a regular user's own request
        # (list_vehicles doesn't bother looking it up there, since it's
        # trivially themselves). Populated for an admin's request, where
        # this endpoint returns every vehicle in the system: without this,
        # every row here showed no owner at all under a section literally
        # titled "Your vehicles" (AccountView), which for an admin reads as
        # if the whole fleet belonged to them.
        "owner_name":  (f"{owner.first_name} {owner.last_name}".strip() or owner.email) if owner else None,
        "owner_email": owner.email if owner else None,
    }


# ── Bonus: GET /api/vehicles  (Dashboard) ─────────────────────────────────────

@router.get("/vehicles")
async def list_vehicles(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return vehicles (with live device status) belonging to the authenticated
    user — or, for admins, every vehicle in the system, matching the
    admin-sees-everything pattern GET /api/devices already uses.
    """
    query = db.query(Vehicle)
    is_admin = user.role in REQUIRE_ADMIN_ROLES
    if not is_admin:
        query = query.filter(Vehicle.clerk_user_id == user.clerk_user_id)

    vehicles = query.all()

    # Only worth the batch-load for an admin's fleet-wide view — a regular
    # user's own vehicles are trivially their own, no lookup needed (and
    # _serialize_vehicle's owner_name/owner_email are simply left None for
    # them, same as before this admin-owner fix).
    owner_by_clerk_id = {}
    if is_admin and vehicles:
        clerk_ids = {v.clerk_user_id for v in vehicles}
        owner_by_clerk_id = {
            u.clerk_user_id: u
            for u in db.query(User).filter(User.clerk_user_id.in_(clerk_ids)).all()
        }

    return {
        "vehicles": [
            _serialize_vehicle(v, owner_by_clerk_id.get(v.clerk_user_id))
            for v in vehicles
        ]
    }


# ── PUT /api/vehicles/{id}  — rename (nickname only) ──────────────────────────

@router.put("/vehicles/{vehicle_id}")
async def update_vehicle(
    vehicle_id: int,
    body: VehicleUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Rename a vehicle. `nickname` is the only editable field here —
    plate/make/model stay immutable via this endpoint (making those
    editable later is a deliberate future decision, not a default), and
    deviceImei/device re-linking is intentionally excluded — that's a
    separate re-pairing flow, not a plain edit.
    """
    nickname = body.nickname.strip()
    if not nickname:
        raise HTTPException(status_code=400, detail="nickname is required")

    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    require_vehicle_access(vehicle, user)

    try:
        vehicle.nickname = nickname
        vehicle.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(vehicle)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error updating vehicle id=%d: %s", vehicle_id, exc)
        raise HTTPException(status_code=500, detail="Database error")

    logger.info("Vehicle %d nickname updated by %s", vehicle_id, user.clerk_user_id)
    return _serialize_vehicle(vehicle)


# ── DELETE /api/vehicles/{id}  — remove + release device to inventory ─────────

@router.delete("/vehicles/{vehicle_id}", status_code=204)
async def delete_vehicle(
    vehicle_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Delete a vehicle registration.

    The linked device (if any) is released back to company inventory —
    same unlink pattern as the admin-only POST /api/devices/{id}/unassign
    (user_id cleared, lifecycle -> in_stock, fresh pairing PIN) — rather
    than left dangling on a deleted Vehicle row. Deleting the vehicle is
    how an owner gives up the device through this flow; re-pairing it (by
    them or anyone else) requires the new PIN.
    """
    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    require_vehicle_access(vehicle, user)

    device = vehicle.device
    try:
        db.delete(vehicle)
        if device is not None:
            _release_device_to_inventory(device)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error deleting vehicle id=%d: %s", vehicle_id, exc)
        raise HTTPException(status_code=500, detail="Database error")

    logger.info(
        "Vehicle %d deleted by %s (device %s released to inventory)",
        vehicle_id, user.clerk_user_id, device.imei if device else "none",
    )
    return None

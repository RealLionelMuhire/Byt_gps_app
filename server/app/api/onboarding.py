"""
Onboarding API endpoints — supports the mobile app's 5-step onboarding flow.

Routes:
    POST /api/users              — Step 4: upsert user profile
    POST /api/devices/pair       — Step 5: pair GPS device by IMEI
    GET  /api/devices/{imei}/status — Step 6: poll for first signal
    POST /api/vehicles           — Step 7: register vehicle
    POST /api/payments/verify    — Step 8 (paid): server-side Flutterwave verify
    POST /api/subscriptions      — Step 8: activate plan

All routes require a valid Clerk Bearer token (via require_auth dependency).
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from app.core.database import get_db
from app.core.auth import require_auth
from app.core.config import settings
from app.models.user import User
from app.models.device import Device
from app.models.vehicle import Vehicle
from app.models.subscription import Subscription, Payment

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Config ────────────────────────────────────────────────────────────────────

PLAN_PRICES = {"trial": 0, "basic": 5000, "fleet": 15000}   # RWF
PLAN_DAYS   = {"trial": 14, "basic": 30,  "fleet": 30}
PLAN_VEHICLES = {"trial": 1, "basic": 3, "fleet": None} # None = unlimited


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


class PaymentVerifyRequest(BaseModel):
    txRef:  str
    planId: str


class PaymentVerifyResponse(BaseModel):
    verified: bool
    status:   Optional[str] = None
    error:    Optional[str] = None


class SubscriptionRequest(BaseModel):
    planId: str


class SubscriptionResponse(BaseModel):
    subscriptionId: int
    expiresAt:      datetime


class SubscriptionUpgradeRequest(BaseModel):
    planId: str
    txRef: str


class PaymentRecord(BaseModel):
    txRef: str
    planId: str
    amount: float
    status: str
    createdAt: datetime

    class Config:
        from_attributes = True


class BillingResponse(BaseModel):
    currentPlan: str
    expiresAt: Optional[datetime]
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

        if existing:
            # Idempotent update
            existing.first_name = body.firstName
            existing.last_name  = body.lastName
            existing.email      = body.email
            existing.role       = body.role or "owner"
            existing.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(existing)
            logger.info("Updated user profile: %s", clerk_user_id)
            return UserResponse(userId=existing.id, alreadyExists=True)

        # First user gets admin flag
        is_first = db.query(User).count() == 0

        user = User(
            clerk_user_id=clerk_user_id,
            first_name=body.firstName,
            last_name=body.lastName,
            email=body.email,
            role=body.role or "owner",
            is_admin=is_first,
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
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """
    Pair an existing GPS device to the authenticated user's account.

    The IMEI must already exist in the `devices` table (pre-loaded by admin).
    Returns 404 if the IMEI is unknown, 409 if already paired to another user.
    """
    imei = body.imei.strip()
    if not imei.isdigit() or len(imei) != 15:
        raise HTTPException(status_code=400, detail="Invalid IMEI — must be exactly 15 digits")

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
        raise HTTPException(status_code=409, detail="Device already registered to another account.")

    try:
        device.user_id    = user.id
        device.updated_at = datetime.utcnow()

        user.onboarding_step = 5
        user.updated_at = datetime.utcnow()

        db.commit()
        db.refresh(device)

        logger.info("Paired device IMEI=%s to user %s", imei, clerk_user_id)
        return {"deviceId": device.id, "status": device.status, "imei": device.imei}

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
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """
    Register a vehicle and link it to the paired GPS device.
    """
    for field, value in [("nickname", body.nickname), ("plate", body.plate),
                         ("make", body.make), ("model", body.model)]:
        if not value or not value.strip():
            raise HTTPException(status_code=400, detail=f"{field} is required")

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Verify the IMEI belongs to this user
    device = db.query(Device).filter(
        Device.imei == body.deviceImei,
        Device.user_id == user.id,
    ).first()
    if not device:
        raise HTTPException(status_code=403, detail="Device not paired to your account")

    # Enforce plan limits
    sub = db.query(Subscription).filter(
        Subscription.clerk_user_id == clerk_user_id,
        Subscription.status == "active"
    ).first()
    
    current_plan = sub.plan_id if sub else "trial"
    vehicle_limit = PLAN_VEHICLES.get(current_plan, 1)

    if vehicle_limit is not None:
        current_vehicles_count = db.query(Vehicle).filter(Vehicle.clerk_user_id == clerk_user_id).count()
        if current_vehicles_count >= vehicle_limit:
            raise HTTPException(
                status_code=403, 
                detail=f"Your {current_plan} plan only allows up to {vehicle_limit} vehicle(s). Please upgrade your plan."
            )

    try:
        vehicle = Vehicle(
            clerk_user_id=clerk_user_id,
            device_id=device.id,
            nickname=body.nickname.strip(),
            plate=body.plate.strip().upper(),
            make=body.make.strip(),
            model=body.model.strip(),
        )
        db.add(vehicle)

        user.onboarding_step = 7
        user.updated_at = datetime.utcnow()

        db.commit()
        db.refresh(vehicle)

        logger.info("Vehicle created id=%d for user %s", vehicle.id, clerk_user_id)
        return VehicleResponse(vehicleId=vehicle.id)

    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error creating vehicle: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


# ── Endpoint 5: POST /api/payments/verify  (Step 8, paid plans) ───────────────

@router.post("/payments/verify", response_model=PaymentVerifyResponse)
async def verify_payment(
    body: PaymentVerifyRequest,
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """
    Server-side Flutterwave payment verification.

    NEVER trust client-side success alone.
    This calls the Flutterwave API with the secret key and verifies:
      - transaction status is "successful"
      - currency is RWF
      - amount matches the plan price

    Requires FLUTTERWAVE_SECRET_KEY in server .env (never in the app).
    """
    if body.planId not in PLAN_PRICES:
        raise HTTPException(status_code=400, detail="Invalid planId")

    if not settings.FLUTTERWAVE_SECRET_KEY:
        logger.warning("FLUTTERWAVE_SECRET_KEY not set — skipping live verification (test mode)")
        # In test mode, trust the client (remove this branch in production)
        return PaymentVerifyResponse(verified=True, status="successful")

    url = f"https://api.flutterwave.com/v3/transactions/verify_by_reference?tx_ref={body.txRef}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {settings.FLUTTERWAVE_SECRET_KEY}"},
            )
            fw = resp.json()
    except Exception as exc:
        logger.error("Flutterwave API error: %s", exc)
        raise HTTPException(status_code=502, detail="Could not reach Flutterwave. Retry later.")

    data = fw.get("data", {})
    amount_ok  = data.get("amount", 0) >= PLAN_PRICES[body.planId]
    status_ok  = data.get("status") == "successful"
    currency_ok = data.get("currency") == "RWF"

    if not (fw.get("status") == "success" and status_ok and currency_ok and amount_ok):
        logger.warning("Payment verification FAILED for txRef=%s: %s", body.txRef, fw)
        return PaymentVerifyResponse(verified=False, error="Payment verification failed")

    # Persist payment record (upsert on tx_ref to prevent duplicates)
    try:
        existing_payment = db.query(Payment).filter(Payment.tx_ref == body.txRef).first()
        if not existing_payment:
            payment = Payment(
                clerk_user_id=clerk_user_id,
                tx_ref=body.txRef,
                plan_id=body.planId,
                amount=data.get("amount", 0),
                currency="RWF",
                status="successful",
                verified_at=datetime.utcnow(),
            )
            db.add(payment)
            db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error saving payment: %s", exc)
        # Non-fatal: verification already passed, activation can still proceed

    logger.info("Payment verified: txRef=%s planId=%s user=%s", body.txRef, body.planId, clerk_user_id)
    return PaymentVerifyResponse(verified=True, status="successful")


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
    if body.planId not in PLAN_PRICES:
        raise HTTPException(status_code=400, detail="Invalid planId")

    # Prevent trial re-use
    if body.planId == "trial":
        used = db.query(Subscription).filter(
            Subscription.clerk_user_id == clerk_user_id
        ).first()
        if used:
            raise HTTPException(status_code=409, detail="Free trial already used")

    expires_at = datetime.utcnow() + timedelta(days=PLAN_DAYS[body.planId])

    try:
        subscription = Subscription(
            clerk_user_id=clerk_user_id,
            plan_id=body.planId,
            status="active",
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
            "Subscription activated: planId=%s user=%s expires=%s",
            body.planId, clerk_user_id, expires_at.isoformat(),
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
    if body.planId not in PLAN_PRICES:
        raise HTTPException(status_code=400, detail="Invalid planId")

    # 1. Confirm payment record exists and is successful
    payment = db.query(Payment).filter(
        Payment.tx_ref == body.txRef,
        Payment.clerk_user_id == clerk_user_id,
        Payment.status == "successful"
    ).first()
    
    if not payment:
        raise HTTPException(status_code=402, detail="Payment not verified or not found")

    # 2. Confirm upgrade (no downgrade allowed)
    current_sub = db.query(Subscription).filter(
        Subscription.clerk_user_id == clerk_user_id,
        Subscription.status == "active"
    ).first()

    if current_sub and PLAN_PRICES[body.planId] <= PLAN_PRICES[current_sub.plan_id]:
        raise HTTPException(status_code=400, detail="Cannot downgrade. Choose a higher plan.")

    # 3. Cancel current, create new
    try:
        if current_sub:
            current_sub.status = "cancelled"
            current_sub.updated_at = datetime.utcnow()

        expires_at = datetime.utcnow() + timedelta(days=PLAN_DAYS[body.planId])
        
        new_sub = Subscription(
            clerk_user_id=clerk_user_id,
            plan_id=body.planId,
            status="active",
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

        logger.info("Subscription upgraded: planId=%s user=%s expires=%s", body.planId, clerk_user_id, expires_at.isoformat())
        return SubscriptionResponse(subscriptionId=new_sub.id, expiresAt=expires_at)

    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error upgrading subscription: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


# ── Endpoint 8: GET /api/billing ──────────────────────────────────────────────

@router.get("/billing", response_model=BillingResponse)
async def get_billing_history(
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """
    Get current active plan and payment history for the user.
    """
    sub = db.query(Subscription).filter(
        Subscription.clerk_user_id == clerk_user_id,
        Subscription.status == "active"
    ).first()

    payments = db.query(Payment).filter(
        Payment.clerk_user_id == clerk_user_id
    ).order_by(Payment.created_at.desc()).limit(20).all()

    payment_records = []
    for p in payments:
        payment_records.append(PaymentRecord(
            txRef=p.tx_ref,
            planId=p.plan_id,
            amount=p.amount,
            status=p.status,
            createdAt=p.created_at
        ))

    return BillingResponse(
        currentPlan=sub.plan_id if sub else "trial",
        expiresAt=sub.expires_at if sub else None,
        payments=payment_records
    )


# ── Bonus: GET /api/vehicles  (Dashboard) ─────────────────────────────────────

@router.get("/vehicles")
async def list_vehicles(
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """
    Return all vehicles (with live device status) belonging to the authenticated user.
    """
    vehicles = (
        db.query(Vehicle)
        .filter(Vehicle.clerk_user_id == clerk_user_id)
        .all()
    )

    result = []
    for v in vehicles:
        device = v.device
        result.append({
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
        })

    return {"vehicles": result}

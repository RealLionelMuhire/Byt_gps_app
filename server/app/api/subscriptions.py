"""
Subscription Plan API — admin-configurable subscription schemes.

A plan defines:
  - billing_type: 'one_time' | 'recurrent'
  - price + currency (e.g. 5000 RWF)
  - duration (e.g. 1 month) — for one_time this is how long it lasts, for
    recurrent it is the billing period the price applies to
  - max_devices (None = unlimited)

Admins create/manage plans here; the mobile app lists active plans via
GET /api/subscription-plans. Plans can be linked to GPS devices
(see PUT /api/devices/{device_id}/plan).
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import require_auth, require_admin, get_current_user
from app.models.subscription import SubscriptionPlan, Subscription
from app.models.user import User
from app.models.company import Company, Membership, CompanyRole

logger = logging.getLogger(__name__)
router = APIRouter()
admin_router = APIRouter()

VALID_BILLING_TYPES = {"prepaid", "postpaid"}
VALID_DURATION_UNITS = {"day", "week", "month", "year"}


# ── Shared plan resolution (used by the onboarding/billing flow too) ──────────

# Legacy hardcoded pricing — used only as a fallback when a plan with the
# given slug doesn't exist in the DB yet (e.g. before migration 014 runs).
FALLBACK_PLANS = {
    "trial": {"price": 0, "days": 14, "max_devices": 1, "currency": "RWF", "pricing_model": "flat", "min_devices": 1},
    "basic": {"price": 5000, "days": 30, "max_devices": 3, "currency": "RWF", "pricing_model": "per_device", "min_devices": 1},
    "fleet": {"price": 15000, "days": 30, "max_devices": None, "currency": "RWF", "pricing_model": "flat", "min_devices": 5},
}


def get_plan_by_slug(
    db: Session, slug: str, include_inactive: bool = False
) -> Optional[SubscriptionPlan]:
    """Look up a plan by slug (case-insensitive).

    By default only ACTIVE plans match — the pricing screen / purchase flow
    must not see deactivated schemes. Pass `include_inactive=True` when you
    need to grandfather clients already on a deactivated plan (limits,
    billing history)."""
    if not slug:
        return None
    query = db.query(SubscriptionPlan).filter(
        func.lower(SubscriptionPlan.slug) == slug.strip().lower()
    )
    if not include_inactive:
        query = query.filter(SubscriptionPlan.is_active == True)
    return query.first()


def plan_purchasable(db: Session, slug: str) -> bool:
    """True if the slug may be purchased/activated right now: an ACTIVE DB
    plan, or a legacy fallback slug (which has no DB row to deactivate).
    Deactivated plans return False so the mobile app can't buy a scheme the
    admin has switched off."""
    plan = get_plan_by_slug(db, slug, include_inactive=True)
    if plan is not None:
        return plan.is_active
    return slug in FALLBACK_PLANS


def plan_config(db: Session, slug: str) -> dict:
    """Resolve a plan's effective config for EXISTING subscribers: the
    admin-configured DB plan wins even if deactivated (grandfathering — a
    client already on the plan keeps its limits/expiry), legacy hardcoded
    values are the fallback (and the default for unknown slugs is the trial
    config). Used by verify_payment / create_subscription / upgrade /
    vehicle limits so pricing never drifts between the mobile app and the
    admin-configured schemes."""
    plan = get_plan_by_slug(db, slug, include_inactive=True)
    if plan:
        return {
            "price": plan.price,
            "days": plan.duration_days,
            "max_devices": plan.max_devices,
            "currency": plan.currency,
            "pricing_model": plan.pricing_model,
            "min_devices": plan.min_devices,
        }
    return FALLBACK_PLANS.get(slug, FALLBACK_PLANS["trial"]).copy()


# ── Schemas ──────────────────────────────────────────────────────────────────

VALID_PRICING_MODELS = {"per_device", "flat"}


class SubscriptionPlanCreate(BaseModel):
    name: str
    slug: str
    billing_type: str = "prepaid"  # prepaid | postpaid
    pricing_model: str = "flat"  # per_device | flat
    price: float = 0.0
    currency: str = "RWF"
    duration_value: int = 1
    duration_unit: str = "month"
    min_devices: int = 1  # minimum devices required to choose this plan
    max_devices: Optional[int] = None  # None = unlimited
    description: Optional[str] = None
    is_active: bool = True

    @field_validator("billing_type")
    @classmethod
    def billing_type_valid(cls, v):
        if v not in VALID_BILLING_TYPES:
            raise ValueError(f"billing_type must be one of: {', '.join(sorted(VALID_BILLING_TYPES))}")
        return v

    @field_validator("pricing_model")
    @classmethod
    def pricing_model_valid(cls, v):
        if v not in VALID_PRICING_MODELS:
            raise ValueError(f"pricing_model must be one of: {', '.join(sorted(VALID_PRICING_MODELS))}")
        return v

    @field_validator("duration_unit")
    @classmethod
    def duration_unit_valid(cls, v):
        if v not in VALID_DURATION_UNITS:
            raise ValueError(f"duration_unit must be one of: {', '.join(sorted(VALID_DURATION_UNITS))}")
        return v

    @field_validator("duration_value")
    @classmethod
    def duration_value_valid(cls, v):
        if v < 1 or v > 36500:
            raise ValueError("duration_value must be between 1 and 36500")
        return v

    @field_validator("price")
    @classmethod
    def price_valid(cls, v):
        if v < 0:
            raise ValueError("price cannot be negative")
        return v


class SubscriptionPlanUpdate(BaseModel):
    """Partial update — only provided fields are changed."""
    name: Optional[str] = None
    billing_type: Optional[str] = None
    pricing_model: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    duration_value: Optional[int] = None
    duration_unit: Optional[str] = None
    min_devices: Optional[int] = None
    max_devices: Optional[int] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("billing_type")
    @classmethod
    def billing_type_valid(cls, v):
        if v is not None and v not in VALID_BILLING_TYPES:
            raise ValueError(f"billing_type must be one of: {', '.join(sorted(VALID_BILLING_TYPES))}")
        return v

    @field_validator("pricing_model")
    @classmethod
    def pricing_model_valid(cls, v):
        if v is not None and v not in VALID_PRICING_MODELS:
            raise ValueError(f"pricing_model must be one of: {', '.join(sorted(VALID_PRICING_MODELS))}")
        return v

    @field_validator("duration_unit")
    @classmethod
    def duration_unit_valid(cls, v):
        if v is not None and v not in VALID_DURATION_UNITS:
            raise ValueError(f"duration_unit must be one of: {', '.join(sorted(VALID_DURATION_UNITS))}")
        return v

    @field_validator("duration_value")
    @classmethod
    def duration_value_valid(cls, v):
        if v is not None and (v < 1 or v > 36500):
            raise ValueError("duration_value must be between 1 and 36500")
        return v

    @field_validator("price")
    @classmethod
    def price_valid(cls, v):
        if v is not None and v < 0:
            raise ValueError("price cannot be negative")
        return v


class SubscriptionPlanResponse(BaseModel):
    id: int
    name: str
    slug: str
    billing_type: str  # prepaid | postpaid
    pricing_model: str  # per_device | flat
    price: float
    currency: str
    duration_value: int
    duration_unit: str
    duration_days: int
    min_devices: int
    max_devices: Optional[int]
    description: Optional[str]
    is_active: bool
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("", response_model=List[SubscriptionPlanResponse])
async def list_subscription_plans(
    include_inactive: bool = Query(False, description="Admin: also return deactivated plans"),
    db: Session = Depends(get_db),
    _: str = Depends(require_auth),
):
    """List subscription plans. Mobile app uses this to render the pricing screen
    (active plans only by default); admins can pass include_inactive=true."""
    query = db.query(SubscriptionPlan)
    if not include_inactive:
        query = query.filter(SubscriptionPlan.is_active == True)
    return query.order_by(SubscriptionPlan.price.asc(), SubscriptionPlan.id.asc()).all()


@router.post("", response_model=SubscriptionPlanResponse, status_code=201)
async def create_subscription_plan(
    body: SubscriptionPlanCreate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Create a new subscription scheme. Admin only."""
    slug = body.slug.strip().lower()
    if not slug:
        raise HTTPException(status_code=400, detail="slug is required")

    existing = db.query(SubscriptionPlan).filter(
        func.lower(SubscriptionPlan.slug) == slug
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"A plan with slug '{slug}' already exists.",
        )

    plan = SubscriptionPlan(
        name=body.name.strip(),
        slug=slug,
        billing_type=body.billing_type,
        pricing_model=body.pricing_model,
        price=body.price,
        currency=body.currency.strip().upper() or "RWF",
        duration_value=body.duration_value,
        duration_unit=body.duration_unit,
        max_devices=body.max_devices,
        description=body.description,
        is_active=body.is_active,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    logger.info("Created subscription plan %s (%s %s/%s %s)",
                plan.slug, plan.price, plan.currency, plan.duration_value, plan.duration_unit)
    return plan


@router.put("/{plan_id}", response_model=SubscriptionPlanResponse)
async def update_subscription_plan(
    plan_id: int,
    body: SubscriptionPlanUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Update a subscription scheme. Admin only."""
    plan = db.query(SubscriptionPlan).filter(SubscriptionPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    # Columns that are NOT NULL in the DB — an explicit null for these must be
    # ignored (clearing them would violate the constraint on commit).
    non_nullable = {
        "name", "billing_type", "pricing_model", "price", "currency",
        "duration_value", "duration_unit", "is_active",
    }
    for field, value in body.model_dump(exclude_unset=True).items():
        # exclude_unset guarantees absent fields stay untouched, so an explicit
        # null here means "clear this field" for nullable columns like
        # description / max_devices.
        if value is None and field in non_nullable:
            continue
        if field == "currency" and value:
            value = value.strip().upper()
        setattr(plan, field, value)

    db.commit()
    db.refresh(plan)
    logger.info("Updated subscription plan id=%d (%s)", plan.id, plan.slug)
    return plan


@router.delete("/{plan_id}", status_code=204)
async def deactivate_subscription_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Soft-delete (deactivate) a subscription scheme. Admin only.

    Deactivated plans no longer appear on the mobile pricing screen but remain
    attached to devices already linked to them. Use PUT to reactivate.
    """
    plan = db.query(SubscriptionPlan).filter(SubscriptionPlan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    plan.is_active = False
    db.commit()
    logger.info("Deactivated subscription plan id=%d (%s)", plan.id, plan.slug)
    return None


# ── Admin Subscription Management ───────────────────────────────────────────
# Admins create subscription records for companies.
# Companies then choose/activate from available plans.


class AdminSubscriptionCreate(BaseModel):
    """Admin creates a subscription for a company.
    expires_at is calculated from plan duration — not provided."""
    company_id: int  # target company
    plan_slug: str  # e.g. 'basic', 'fleet'
    price: Optional[float] = None  # override plan price, or use plan default


class AdminSubscriptionResponse(BaseModel):
    id: int
    companyId: Optional[int] = None
    clerkUserId: Optional[str] = None
    planId: str
    billingType: str
    pricingModel: str
    deviceCountSnapshot: int
    status: str
    price: float
    amountDue: float
    paymentStatus: str
    dueDate: Optional[datetime] = None
    startedAt: Optional[datetime] = None
    expiresAt: datetime
    createdAt: datetime

    class Config:
        from_attributes = True


@admin_router.post("/subscriptions", response_model=AdminSubscriptionResponse, status_code=201)
async def admin_create_subscription(
    body: AdminSubscriptionCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """
    Admin creates a subscription for a company.
    expires_at is calculated from plan duration.
    amount_due is calculated from pricing_model × device count.
    """
    company = db.query(Company).filter(Company.id == body.company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    plan = get_plan_by_slug(db, body.plan_slug, include_inactive=True)
    if not plan:
        raise HTTPException(status_code=404, detail=f"Plan '{body.plan_slug}' not found.")

    # Count devices for this company
    from app.models.device import Device
    device_count = db.query(Device).filter(Device.company_id == company.id).count()

    # Enforce min/max device limits
    if device_count < plan.min_devices:
        raise HTTPException(
            status_code=400,
            detail=f"This plan requires at least {plan.min_devices} device(s). Company has {device_count}.",
        )
    if plan.max_devices is not None and device_count > plan.max_devices:
        raise HTTPException(
            status_code=400,
            detail=f"This plan allows at most {plan.max_devices} device(s). Company has {device_count}.",
        )

    # Calculate price based on pricing model
    if body.price is not None:
        unit_price = body.price  # explicit admin override
    else:
        unit_price = plan.price

    # Calculate amount due
    if plan.pricing_model == "per_device":
        amount_due = unit_price * max(device_count, 1)
    else:
        amount_due = unit_price

    started_at = datetime.utcnow()
    expires_at = started_at + timedelta(days=plan.duration_days)

    # For postpaid: due_date is end of billing period
    due_date = expires_at if plan.billing_type == "postpaid" else None

    subscription = Subscription(
        company_id=company.id,
        clerk_user_id=admin.clerk_user_id,  # admin who created it
        plan_id=body.plan_slug,
        billing_type=plan.billing_type,
        pricing_model=plan.pricing_model,
        device_count_snapshot=device_count,
        status="active",
        price=unit_price,
        amount_due=amount_due,
        payment_status="pending" if plan.billing_type == "postpaid" else "paid",
        due_date=due_date,
        started_at=started_at,
        expires_at=expires_at,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)

    logger.info(
        "Admin %d created subscription for company %d (plan=%s, devices=%d, amount=%.2f, expires=%s)",
        admin.id, company.id, body.plan_slug, device_count, amount_due, expires_at,
    )

    return AdminSubscriptionResponse(
        id=subscription.id,
        companyId=subscription.company_id,
        clerkUserId=subscription.clerk_user_id,
        planId=subscription.plan_id,
        billingType=subscription.billing_type,
        pricingModel=subscription.pricing_model,
        deviceCountSnapshot=subscription.device_count_snapshot,
        status=subscription.status,
        price=subscription.price,
        amountDue=subscription.amount_due,
        paymentStatus=subscription.payment_status,
        dueDate=subscription.due_date,
        startedAt=subscription.started_at,
        expiresAt=subscription.expires_at,
        createdAt=subscription.created_at,
    )


@admin_router.get("/subscriptions", response_model=List[AdminSubscriptionResponse])
async def admin_list_subscriptions(
    company_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    payment_status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    List all subscriptions. Admin only.
    Filter by company_id, status, and/or payment_status.
    """
    query = db.query(Subscription)
    if company_id:
        query = query.filter(Subscription.company_id == company_id)
    if status:
        query = query.filter(Subscription.status == status)
    if payment_status:
        query = query.filter(Subscription.payment_status == payment_status)
    subs = query.order_by(Subscription.created_at.desc()).all()

    return [
        AdminSubscriptionResponse(
            id=sub.id,
            companyId=sub.company_id,
            clerkUserId=sub.clerk_user_id,
            planId=sub.plan_id,
            billingType=sub.billing_type,
            pricingModel=sub.pricing_model,
            deviceCountSnapshot=sub.device_count_snapshot,
            status=sub.status,
            price=sub.price,
            amountDue=sub.amount_due,
            paymentStatus=sub.payment_status,
            dueDate=sub.due_date,
            startedAt=sub.started_at,
            expiresAt=sub.expires_at,
            createdAt=sub.created_at,
        )
        for sub in subs
    ]


@admin_router.put("/subscriptions/{sub_id}", response_model=AdminSubscriptionResponse)
async def admin_update_subscription(
    sub_id: int,
    body: SubscriptionPlanUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Admin updates a subscription (e.g. mark as paid, extend expiry).
    """
    sub = db.query(Subscription).filter(Subscription.id == sub_id).first()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found.")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(sub, field, value)
    sub.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(sub)

    return AdminSubscriptionResponse(
        id=sub.id,
        companyId=sub.company_id,
        clerkUserId=sub.clerk_user_id,
        planId=sub.plan_id,
        billingType=sub.billing_type,
        pricingModel=sub.pricing_model,
        deviceCountSnapshot=sub.device_count_snapshot,
        status=sub.status,
        price=sub.price,
        amountDue=sub.amount_due,
        paymentStatus=sub.payment_status,
        dueDate=sub.due_date,
        startedAt=sub.started_at,
        expiresAt=sub.expires_at,
        createdAt=sub.created_at,
    )


@admin_router.delete("/subscriptions/{sub_id}", status_code=200)
async def admin_cancel_subscription(
    sub_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """
    Admin cancels a subscription.
    """
    sub = db.query(Subscription).filter(Subscription.id == sub_id).first()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found.")

    sub.status = "cancelled"
    sub.updated_at = datetime.utcnow()
    db.commit()

    logger.info("Admin cancelled subscription %d for company %s", sub_id, sub.company_id)
    return {"message": "Subscription cancelled.", "id": sub_id}


# ── Company Billing Summary ──────────────────────────────────────────────────

class BillingSummaryResponse(BaseModel):
    companyId: int
    companyName: str
    activeSubscription: Optional[dict] = None
    totalDevices: int
    billingSummary: dict
    paymentHistory: List[dict]


@router.get("/billing/{company_id}", response_model=BillingSummaryResponse)
async def get_company_billing_summary(
    company_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Get billing summary for a company.
    Shows what the company owes based on their plan, device count, and pricing model.
    """
    # Check user is a member of this company
    membership = db.query(Membership).filter(
        Membership.user_id == user.id,
        Membership.company_id == company_id,
    ).first()
    if not membership and user.role not in ("SUPER_ADMIN", "ADMIN"):
        raise HTTPException(status_code=403, detail="Not a member of this company.")

    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    # Get active subscription
    active_sub = db.query(Subscription).filter(
        Subscription.company_id == company_id,
        Subscription.status == "active",
    ).order_by(Subscription.created_at.desc()).first()

    # Count current devices
    from app.models.device import Device
    device_count = db.query(Device).filter(Device.company_id == company_id).count()

    # Build active subscription info
    active_sub_info = None
    if active_sub:
        active_sub_info = {
            "id": active_sub.id,
            "planId": active_sub.plan_id,
            "billingType": active_sub.billing_type,
            "pricingModel": active_sub.pricing_model,
            "unitPrice": active_sub.price,
            "amountDue": active_sub.amount_due,
            "paymentStatus": active_sub.payment_status,
            "dueDate": active_sub.due_date.isoformat() if active_sub.due_date else None,
            "deviceCountAtSubscription": active_sub.device_count_snapshot,
            "currentDeviceCount": device_count,
            "startedAt": active_sub.started_at.isoformat() if active_sub.started_at else None,
            "expiresAt": active_sub.expires_at.isoformat() if active_sub.expires_at else None,
        }

    # Calculate what they would owe now (in case device count changed)
    if active_sub:
        plan = get_plan_by_slug(db, active_sub.plan_id, include_inactive=True)
        if plan and plan.pricing_model == "per_device":
            recalculated_amount = plan.price * max(device_count, 1)
        elif plan:
            recalculated_amount = plan.price
        else:
            recalculated_amount = active_sub.amount_due
    else:
        recalculated_amount = 0.0

    # Payment history
    from app.models.subscription import Payment
    payments = db.query(Payment).filter(
        Payment.clerk_user_id.in_([
            m.user.clerk_user_id for m in
            db.query(Membership).filter(Membership.company_id == company_id).all()
        ])
    ).order_by(Payment.verified_at.desc()).limit(20).all()

    payment_history = [
        {
            "id": p.id,
            "txRef": p.tx_ref,
            "planId": p.plan_id,
            "amount": p.amount,
            "currency": p.currency,
            "status": p.status,
            "verifiedAt": p.verified_at.isoformat() if p.verified_at else None,
        }
        for p in payments
    ]

    return BillingSummaryResponse(
        companyId=company.id,
        companyName=company.name,
        activeSubscription=active_sub_info,
        totalDevices=device_count,
        billingSummary={
            "currentAmountDue": active_sub.amount_due if active_sub else 0.0,
            "recalculatedAmount": recalculated_amount,
            "deviceCount": device_count,
            "hasActiveSubscription": active_sub is not None,
            "paymentStatus": active_sub.payment_status if active_sub else "none",
        },
        paymentHistory=payment_history,
    )

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
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import require_auth, require_admin
from app.models.subscription import SubscriptionPlan
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_BILLING_TYPES = {"one_time", "recurrent"}
VALID_DURATION_UNITS = {"day", "week", "month", "year"}


# ── Shared plan resolution (used by the onboarding/billing flow too) ──────────

# Legacy hardcoded pricing — used only as a fallback when a plan with the
# given slug doesn't exist in the DB yet (e.g. before migration 014 runs).
FALLBACK_PLANS = {
    "trial": {"price": 0, "days": 14, "max_devices": 1, "currency": "RWF"},
    "basic": {"price": 5000, "days": 30, "max_devices": 3, "currency": "RWF"},
    "fleet": {"price": 15000, "days": 30, "max_devices": None, "currency": "RWF"},
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
        }
    return FALLBACK_PLANS.get(slug, FALLBACK_PLANS["trial"]).copy()


# ── Schemas ──────────────────────────────────────────────────────────────────

class SubscriptionPlanCreate(BaseModel):
    name: str
    slug: str
    billing_type: str = "recurrent"
    price: float = 0.0
    currency: str = "RWF"
    duration_value: int = 1
    duration_unit: str = "month"
    max_devices: Optional[int] = None
    description: Optional[str] = None
    is_active: bool = True

    @field_validator("billing_type")
    @classmethod
    def billing_type_valid(cls, v):
        if v not in VALID_BILLING_TYPES:
            raise ValueError(f"billing_type must be one of: {', '.join(sorted(VALID_BILLING_TYPES))}")
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
    price: Optional[float] = None
    currency: Optional[str] = None
    duration_value: Optional[int] = None
    duration_unit: Optional[str] = None
    max_devices: Optional[int] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("billing_type")
    @classmethod
    def billing_type_valid(cls, v):
        if v is not None and v not in VALID_BILLING_TYPES:
            raise ValueError(f"billing_type must be one of: {', '.join(sorted(VALID_BILLING_TYPES))}")
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
    billing_type: str
    price: float
    currency: str
    duration_value: int
    duration_unit: str
    duration_days: int
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
        "name", "billing_type", "price", "currency",
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

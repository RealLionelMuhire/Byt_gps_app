"""Entitlement endpoints: what the caller's plan grants, and admin
management of the feature catalog / per-plan features.

See app/services/entitlements.py for resolution and checks.
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_admin
from app.core.database import get_db
from app.core.serialization import UtcDateTime
from app.models.entitlement import Feature, PlanFeature
from app.models.subscription import SubscriptionPlan
from app.models.user import User
from app.services.entitlements import (
    FEATURES_BY_KEY, PLAN_COLUMN_FEATURES, _mode, resolve_entitlements,
)

logger = logging.getLogger(__name__)
me_router = APIRouter()
admin_router = APIRouter()


# --- Schemas ---


class GrantResponse(BaseModel):
    enabled: bool
    limit: Optional[int] = None


class EntitlementsPlan(BaseModel):
    id: int
    slug: str
    name: str


class EntitlementsResponse(BaseModel):
    mode: str
    status: str  # active | expired | none | admin
    plan: Optional[EntitlementsPlan] = None
    expires_at: Optional[UtcDateTime] = None
    features: Dict[str, GrantResponse]
    # Vehicles the subscription covers; plan features apply only to these.
    covered_vehicle_ids: List[int] = []


class FeatureResponse(BaseModel):
    key: str
    name: str
    description: Optional[str] = None
    group: str
    kind: str
    unit: Optional[str] = None
    sort_order: int
    is_active: bool
    # vehicles.max: edited via the plan's max_devices, not plan features.
    backed_by_plan_column: bool


class PlanFeatureItem(BaseModel):
    key: str
    enabled: bool = True
    limit: Optional[int] = None


class PlanFeaturesResponse(BaseModel):
    plan_id: int
    plan_slug: str
    features: List[PlanFeatureItem]


class PlanFeaturesUpdate(BaseModel):
    # The plan's COMPLETE feature set — anything not listed is removed.
    features: List[PlanFeatureItem]


# --- Routes ---


@me_router.get("/entitlements", response_model=EntitlementsResponse)
async def get_my_entitlements(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The caller's own plan features — what the app should show as
    available/locked. `mode` tells the client whether denials are real yet
    ("enforce") or only being measured ("log")."""
    ent = resolve_entitlements(db, user)
    return EntitlementsResponse(
        mode=_mode(),
        status=ent.status,
        plan=EntitlementsPlan(id=ent.plan.id, slug=ent.plan.slug, name=ent.plan.name) if ent.plan else None,
        expires_at=ent.expires_at,
        features={
            key: GrantResponse(enabled=ent.denial_reason(key) is None, limit=ent.limit_for(key))
            for key in FEATURES_BY_KEY
        },
        covered_vehicle_ids=sorted(ent.covered_vehicle_ids),
    )


def _feature_response(f: Feature) -> FeatureResponse:
    return FeatureResponse(
        key=f.key, name=f.name, description=f.description, group=f.group, kind=f.kind,
        unit=f.unit, sort_order=f.sort_order, is_active=f.is_active,
        backed_by_plan_column=f.key in PLAN_COLUMN_FEATURES,
    )


@admin_router.get("/features", response_model=List[FeatureResponse])
async def list_features(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """The feature catalog, in display order."""
    rows = db.query(Feature).order_by(Feature.sort_order, Feature.key).all()
    return [_feature_response(f) for f in rows]


def _plan_or_404(db: Session, plan_id: int) -> SubscriptionPlan:
    plan = db.query(SubscriptionPlan).filter(SubscriptionPlan.id == plan_id).first()
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan


def _plan_features_response(db: Session, plan: SubscriptionPlan) -> PlanFeaturesResponse:
    rows = (
        db.query(PlanFeature)
        .join(Feature, Feature.key == PlanFeature.feature_key)
        .filter(PlanFeature.plan_id == plan.id)
        .order_by(Feature.sort_order, Feature.key)
        .all()
    )
    return PlanFeaturesResponse(
        plan_id=plan.id,
        plan_slug=plan.slug,
        features=[PlanFeatureItem(key=r.feature_key, enabled=r.enabled, limit=r.limit_value) for r in rows],
    )


@admin_router.get("/plans/{plan_id}/features", response_model=PlanFeaturesResponse)
async def get_plan_features(
    plan_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    return _plan_features_response(db, _plan_or_404(db, plan_id))


@admin_router.put("/plans/{plan_id}/features", response_model=PlanFeaturesResponse)
async def replace_plan_features(
    plan_id: int,
    body: PlanFeaturesUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Replace a plan's whole feature set in one transaction. Applies to
    everyone currently on the plan (there's no per-subscription snapshot
    yet), which is why it's logged with the before/after keys."""
    plan = _plan_or_404(db, plan_id)

    seen = set()
    for item in body.features:
        if item.key in seen:
            raise HTTPException(status_code=400, detail=f"Duplicate feature: {item.key}")
        seen.add(item.key)
        feature = FEATURES_BY_KEY.get(item.key)
        if feature is None:
            raise HTTPException(status_code=400, detail=f"Unknown feature: {item.key}")
        if item.key in PLAN_COLUMN_FEATURES:
            raise HTTPException(
                status_code=400,
                detail=f"{item.key} is set by the plan's max_devices, not plan features.",
            )
        if item.limit is not None:
            if feature.kind != "limit":
                raise HTTPException(status_code=400, detail=f"{item.key} doesn't take a limit.")
            if item.limit < 0:
                raise HTTPException(status_code=400, detail=f"{item.key}'s limit can't be negative.")

    catalog = {k for (k,) in db.query(Feature.key).filter(Feature.key.in_(seen)).all()} if seen else set()
    missing = seen - catalog
    if missing:
        raise HTTPException(status_code=400, detail=f"Not in the catalog yet: {', '.join(sorted(missing))}")

    before = sorted(
        k for (k,) in db.query(PlanFeature.feature_key)
        .filter(PlanFeature.plan_id == plan.id, PlanFeature.enabled == True)  # noqa: E712
        .all()
    )
    db.query(PlanFeature).filter(PlanFeature.plan_id == plan.id).delete()
    now = datetime.utcnow()
    for item in body.features:
        db.add(PlanFeature(
            plan_id=plan.id, feature_key=item.key, enabled=item.enabled,
            limit_value=item.limit, created_at=now, updated_at=now,
        ))
    db.commit()

    after = sorted(i.key for i in body.features if i.enabled)
    logger.info(
        "Admin %s replaced features of plan %s: removed=%s added=%s",
        admin.clerk_user_id, plan.slug,
        sorted(set(before) - set(after)), sorted(set(after) - set(before)),
    )
    return _plan_features_response(db, plan)

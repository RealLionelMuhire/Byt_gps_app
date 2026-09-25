"""
Plan entitlements: which features a device owner's plan grants, and the
checks that gate routes on them.

Rollout is controlled by settings.ENTITLEMENT_MODE:
  - "off":     no checks at all.
  - "log":     (default) every check runs, but a would-be denial is only
               recorded in entitlement_check_log — the request proceeds.
               Exists so the live effect of a plan configuration can be
               measured before it can block anyone, and because the mobile
               app versions already installed don't understand a
               feature-denial response yet.
  - "enforce": a denial raises 402 with a structured body
               ({"code": "feature_not_in_plan", "feature", "reason", ...}).

Checks are always evaluated against the DEVICE OWNER's plan, not the
caller's — an admin acting on a client's device must be measured against
the client's plan (same rule create_vehicle already follows). Admin callers
(and admin owners) bypass every check.

The feature KEYS live in FEATURES below, in code, because that's where
each one is checked; the features table (migration 047) holds their
editable metadata, and plan_features says which plan includes which.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.auth import REQUIRE_ADMIN_ROLES, get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.models.device import Device
from app.models.entitlement import EntitlementCheckLog, Feature, PlanFeature
from app.models.subscription import SubscriptionPlan
from app.models.user import User
from app.models.vehicle import Vehicle
from app.services.plan_resolution import resolve_owner_plan
from app.services.subscription_billing import covered_vehicle_ids

logger = logging.getLogger(__name__)

MODES = ("off", "log", "enforce")


@dataclass(frozen=True)
class FeatureDef:
    key: str
    name: str
    group: str
    kind: str = "boolean"  # boolean | limit
    unit: Optional[str] = None
    description: str = ""


# Order here is the default display order. Adding a key: add it here, check
# it somewhere, and seed its features row (+ plan_features rows, if existing
# plans should get it) in a migration — tests/test_entitlements.py asserts
# this list and migration 047's seed stay in sync.
FEATURES: List[FeatureDef] = [
    FeatureDef("vehicles.max", "Vehicles", "Fleet", kind="limit", unit="vehicles",
               description="How many vehicles the account can register. Backed by the plan's max_devices."),
    FeatureDef("tracking.live", "Live tracking", "Tracking",
               description="Real-time vehicle position, live road name and the live trail."),
    FeatureDef("history.trips", "Trip history", "History",
               description="Recorded trips, route playback and raw location history."),
    FeatureDef("history.period", "Period history", "History",
               description="Everything a vehicle did over a chosen date range."),
    FeatureDef("history.retention_days", "History retention", "History", kind="limit", unit="days",
               description="How far back history can be viewed."),
    FeatureDef("geofences.enabled", "Geofencing", "Geofencing",
               description="Create zones and get enter/exit alerts."),
    FeatureDef("geofences.max_zones", "Number of zones", "Geofencing", kind="limit", unit="zones",
               description="How many geofence zones the account can have."),
    FeatureDef("geofences.polygon", "Polygon zones", "Geofencing",
               description="Zones drawn as any shape, not only circles."),
    FeatureDef("alerts.push", "Push notifications", "Alerts",
               description="Alarm notifications on the phone."),
    FeatureDef("alerts.history", "Alert history", "Alerts",
               description="The list of past alarms and acknowledging them."),
    FeatureDef("alerts.overspeed", "Overspeed alerts", "Alerts",
               description="Set a speed limit and get alerted when it's exceeded."),
    FeatureDef("alerts.rules", "Alert rules", "Alerts",
               description="Choose which alarm types notify and how."),
    FeatureDef("commands.fuel_cut", "Fuel cut / restore", "Commands",
               description="Remotely cut or restore the vehicle's fuel supply."),
    FeatureDef("commands.alarm_config", "Device alarm settings", "Commands",
               description="Turn the tracker's vibration, power-cut and ignition alarms on or off."),
    FeatureDef("commands.query", "Device queries", "Commands",
               description="Ask the tracker for its current location or status."),
    FeatureDef("commands.raw", "Advanced commands", "Commands",
               description="Send any supported command to the tracker directly."),
    FeatureDef("diagnostics", "Diagnostics", "Diagnostics",
               description="Device health and GPS quality details."),
]
FEATURES_BY_KEY: Dict[str, FeatureDef] = {f.key: f for f in FEATURES}

# Backed by a SubscriptionPlan column instead of a plan_features row — see
# PlanFeature's docstring.
PLAN_COLUMN_FEATURES = {"vehicles.max"}


@dataclass(frozen=True)
class Grant:
    enabled: bool
    limit: Optional[int] = None  # None = unlimited (limit features only)


@dataclass
class Entitlements:
    status: str  # active | expired | none | admin
    plan: Optional[SubscriptionPlan] = None
    expires_at: Optional[datetime] = None
    grants: Dict[str, Grant] = field(default_factory=dict)
    # Vehicles the active subscription covers (migration 048) — every plan
    # is per vehicle, so plan features apply only to these.
    covered_vehicle_ids: set = field(default_factory=set)

    @property
    def is_admin(self) -> bool:
        return self.status == "admin"

    def denial_reason(self, key: str) -> Optional[str]:
        """Why `key` isn't granted, or None if it is."""
        if self.is_admin:
            return None
        if self.status == "none":
            return "no_subscription"
        if self.status == "expired":
            return "subscription_expired"
        grant = self.grants.get(key)
        if grant is None or not grant.enabled:
            return "not_in_plan"
        return None

    def limit_for(self, key: str) -> Optional[int]:
        grant = self.grants.get(key)
        return grant.limit if grant else None


def _is_admin(user: Optional[User]) -> bool:
    return user is not None and user.role in REQUIRE_ADMIN_ROLES


def resolve_entitlements(db: Session, owner: Optional[User]) -> Entitlements:
    """What `owner`'s plan grants right now. Expiry-aware (see
    resolve_owner_plan): an expired subscription grants nothing."""
    if _is_admin(owner):
        return Entitlements(status="admin", grants={f.key: Grant(True) for f in FEATURES})

    resolved = resolve_owner_plan(db, owner)
    if resolved.status != "active" or resolved.plan is None:
        return Entitlements(
            status=resolved.status if resolved.status != "active" else "none",
            plan=resolved.plan,
            expires_at=resolved.subscription.expires_at if resolved.subscription else None,
        )

    plan = resolved.plan
    grants = {
        row.feature_key: Grant(row.enabled, row.limit_value)
        for row in db.query(PlanFeature).filter(PlanFeature.plan_id == plan.id).all()
    }
    grants["vehicles.max"] = Grant(True, plan.max_devices)
    return Entitlements(
        status="active", plan=plan, expires_at=resolved.subscription.expires_at, grants=grants,
        covered_vehicle_ids=covered_vehicle_ids(db, resolved.subscription),
    )


def _vehicle_not_covered(db: Session, entitlements: Entitlements, owner: User, device_id: Optional[int]) -> bool:
    """True when the request is about a device whose vehicle the owner's
    active subscription doesn't cover. A device with no vehicle record
    can't be chosen at checkout, so it isn't held against the owner."""
    if device_id is None or entitlements.status != "active":
        return False
    vehicle = (
        db.query(Vehicle)
        .filter(Vehicle.device_id == device_id, Vehicle.clerk_user_id == owner.clerk_user_id)
        .first()
    )
    return vehicle is not None and vehicle.id not in entitlements.covered_vehicle_ids


def _mode() -> str:
    mode = (settings.ENTITLEMENT_MODE or "log").strip().lower()
    return mode if mode in MODES else "log"


def _record(db: Session, *, owner: User, actor: Optional[User], key: str, reason: str,
            route: str, mode: str) -> None:
    """Upsert today's counter row for this denial. Never raises — a logging
    failure must not turn into a failed request."""
    try:
        now = datetime.utcnow()
        row = (
            db.query(EntitlementCheckLog)
            .filter_by(day=now.date(), owner_user_id=owner.id, feature_key=key, reason=reason, route=route)
            .first()
        )
        if row is None:
            db.add(EntitlementCheckLog(
                day=now.date(), owner_user_id=owner.id, actor_user_id=actor.id if actor else None,
                feature_key=key, reason=reason, route=route, mode=mode,
                count=1, first_seen=now, last_seen=now,
            ))
        else:
            row.count += 1
            row.last_seen = now
            row.mode = mode
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("Could not record entitlement check for %s/%s: %s", owner.id, key, exc)


def _deny(db: Session, *, owner: User, actor: Optional[User], key: str, reason: str,
          route: str, mode: str, limit: Optional[int] = None) -> None:
    _record(db, owner=owner, actor=actor, key=key, reason=reason, route=route, mode=mode)
    if mode == "log":
        logger.info("Entitlement (log only): owner=%s would be denied %s (%s) on %s",
                    owner.id, key, reason, route)
        return
    feature = FEATURES_BY_KEY.get(key)
    raise HTTPException(
        status_code=402,
        detail={
            "code": "feature_not_in_plan",
            "feature": key,
            "feature_name": feature.name if feature else key,
            "reason": reason,
            "limit": limit,
            "message": _denial_message(feature.name if feature else key, reason, limit),
        },
    )


def _denial_message(name: str, reason: str, limit: Optional[int]) -> str:
    if reason == "no_subscription":
        return f"{name} needs an active plan. Choose a plan to continue."
    if reason == "subscription_expired":
        return f"Your plan has expired. Renew it to use {name}."
    if reason == "over_limit":
        return f"Your plan allows up to {limit} for {name}. Upgrade to add more."
    if reason == "vehicle_not_covered":
        return f"This vehicle isn't on your plan yet. Add it to your plan to use {name}."
    return f"{name} isn't included in your plan. Upgrade to use it."


def _safe_resolve(db: Session, owner: User) -> Optional[Entitlements]:
    """resolve_entitlements, failing OPEN: if it can't be resolved (e.g. a
    deploy went out before migration 047 was applied — this project's
    recurring 040/042/043 failure mode), log loudly and skip the check
    rather than turn every gated route into a 500."""
    try:
        return resolve_entitlements(db, owner)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("Entitlement resolution failed for owner=%s — check skipped (failing open): %s",
                     owner.id, exc)
        return None


def check_feature(db: Session, *, owner: Optional[User], actor: Optional[User], key: str,
                  route: str, device_id: Optional[int] = None) -> None:
    """Gate on a boolean feature. Log-only mode records and returns; enforce
    mode raises 402. No-op when there's no owner to measure against.

    Recording a denial COMMITS `db` — call this before the handler has
    staged any writes of its own (every current call site does), or those
    writes would be committed early."""
    mode = _mode()
    if mode == "off" or owner is None or _is_admin(actor):
        return
    entitlements = _safe_resolve(db, owner)
    if entitlements is None:
        return
    reason = entitlements.denial_reason(key)
    if reason is None and _vehicle_not_covered(db, entitlements, owner, device_id):
        reason = "vehicle_not_covered"
    if reason:
        _deny(db, owner=owner, actor=actor, key=key, reason=reason, route=route, mode=mode)


def check_limit(db: Session, *, owner: Optional[User], actor: Optional[User], key: str,
                current: int, route: str) -> None:
    """Gate adding one more of something against a limit feature: denied
    when `current` (the count BEFORE adding) has already reached the
    limit. A NULL limit is unlimited. Same commit caveat as check_feature."""
    mode = _mode()
    if mode == "off" or owner is None or _is_admin(actor):
        return
    entitlements = _safe_resolve(db, owner)
    if entitlements is None:
        return
    reason = entitlements.denial_reason(key)
    limit = entitlements.limit_for(key)
    if reason is None and limit is not None and current >= limit:
        reason = "over_limit"
    if reason:
        _deny(db, owner=owner, actor=actor, key=key, reason=reason, route=route, mode=mode, limit=limit)


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    return f"{request.method} {path}"


def _owner_for_request(db: Session, request: Request, user: User) -> tuple:
    """(owner, device_id): the device owner when the route is about a device
    (device_id in the path or query), otherwise (caller, None). Owner None
    when the device doesn't exist or has no owner — the route itself will
    404/403 as it always has."""
    raw = request.path_params.get("device_id") or request.query_params.get("device_id")
    if raw is None:
        return user, None
    try:
        device_id = int(raw)
    except (TypeError, ValueError):
        return None, None
    device = db.query(Device).filter(Device.id == device_id).first()
    if device is None or device.user_id is None:
        return None, None
    if device.user_id == user.id:
        return user, device_id
    return db.query(User).filter(User.id == device.user_id).first(), device_id


def require_feature(key: str):
    """Route dependency: `@router.get(..., dependencies=[require_feature("x")])`.

    Uses the same get_current_user/get_db callables as the routes
    themselves, so FastAPI's per-request dependency cache means no second
    auth round-trip or DB session."""
    if key not in FEATURES_BY_KEY:
        raise ValueError(f"Unknown feature key: {key}")

    async def _check(
        request: Request,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> None:
        owner, device_id = _owner_for_request(db, request, user)
        check_feature(db, owner=owner, actor=user, key=key, route=_route_label(request), device_id=device_id)

    return Depends(_check)


def seed_plan_features(db: Session, plan: SubscriptionPlan) -> None:
    """Grant a newly created plan every catalog feature, unlimited — the
    same as every pre-existing plan got in migration 047. Until the admin
    plan builder exists, a new plan must not silently lose features that
    every other plan has. Caller commits."""
    existing = {
        k for (k,) in db.query(PlanFeature.feature_key).filter(PlanFeature.plan_id == plan.id).all()
    }
    catalog_keys = {k for (k,) in db.query(Feature.key).filter(Feature.is_active == True).all()}  # noqa: E712
    for key in sorted(catalog_keys - PLAN_COLUMN_FEATURES - existing):
        db.add(PlanFeature(plan_id=plan.id, feature_key=key, enabled=True, limit_value=None))

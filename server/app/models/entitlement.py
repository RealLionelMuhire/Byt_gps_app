"""Feature catalog + per-plan feature grants (migration 047).

See app/services/entitlements.py for how these are resolved and checked.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean, Column, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


class Feature(Base):
    """
    One entry in the feature catalog — something a plan can include.

    Which keys EXIST (and their kind) is defined in code, in
    app/services/entitlements.py's FEATURES registry, because that's where
    they're checked; a key with no check behind it would be a plan promise
    nothing enforces. This table holds the admin-facing metadata (name,
    description, grouping, ordering) so it can be edited without a deploy,
    and is what plan_features rows point at.

    kind:
      - "boolean": the plan either includes it or not.
      - "limit":   a numeric ceiling (plan_features.limit_value, NULL =
                   unlimited), e.g. geofences.max_zones.
    """
    __tablename__ = "features"

    key = Column(String(64), primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    group = Column("group_name", String(50), nullable=False)
    kind = Column(String(20), nullable=False, default="boolean")
    unit = Column(String(20), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PlanFeature(Base):
    """
    One feature granted by one plan. No row = the plan does not include the
    feature. For "limit" features, limit_value NULL means unlimited.

    vehicles.max is the one exception: it's still backed by
    SubscriptionPlan.max_devices (already enforced by create_vehicle and
    editable in every existing admin UI), so it never gets a row here —
    two editable sources for the same number would drift.
    """
    __tablename__ = "plan_features"
    __table_args__ = (UniqueConstraint("plan_id", "feature_key", name="uq_plan_features_plan_feature"),)

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("subscription_plans.id", ondelete="CASCADE"), nullable=False, index=True)
    feature_key = Column(String(64), ForeignKey("features.key", ondelete="CASCADE"), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    limit_value = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    feature = relationship("Feature")


class EntitlementCheckLog(Base):
    """
    Would-be (log mode) or actual (enforce mode) denials, aggregated per day
    per (owner, feature, reason, route) — one row with a counter, not one
    row per request, so a client polling an endpoint can't flood the table.

    This is the evidence log-only mode exists to collect: before switching
    ENTITLEMENT_MODE to "enforce", query it to see exactly who would have
    been blocked from what, and confirm no paying customer is on the list.
    """
    __tablename__ = "entitlement_check_log"
    __table_args__ = (
        UniqueConstraint(
            "day", "owner_user_id", "feature_key", "reason", "route",
            name="uq_entitlement_check_log_bucket",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    day = Column(Date, nullable=False)
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_user_id = Column(Integer, nullable=True)
    feature_key = Column(String(64), nullable=False)
    reason = Column(String(40), nullable=False)
    route = Column(String(200), nullable=False)
    mode = Column(String(10), nullable=False)
    count = Column(Integer, nullable=False, default=1)
    first_seen = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen = Column(DateTime, nullable=False, default=datetime.utcnow)

from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean, ForeignKey, JSON, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime
from app.core.database import Base


class SubscriptionPlan(Base):
    """
    Admin-configurable subscription scheme.

    - billing_type: 'one_time' (single payment, length = how long it lasts)
                    | 'recurrent' (recurring — price is charged per length)
    - price + currency: e.g. 5000 RWF per month
    - duration_value + duration_unit: e.g. 1 month, 14 days, 1 year
    - max_devices: None = unlimited (used for plan limits on vehicle creation)

    The `slug` is the identifier the mobile app sends as `planId`
    (trial / basic / fleet by default).
    """
    __tablename__ = "subscription_plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    slug = Column(String(50), unique=True, nullable=False, index=True)
    billing_type = Column(String(20), nullable=False, default="recurrent")  # one_time | recurrent
    billing_model = Column(String(20), nullable=False, default="prepaid")   # prepaid | postpaid
    charge_scope = Column(String(20), nullable=False, default="per_device") # per_device | flat (all plans per vehicle since migration 048)
    price = Column(Float, nullable=False, default=0.0)
    currency = Column(String(10), nullable=False, default="RWF")
    duration_value = Column(Integer, nullable=False, default=1)
    duration_unit = Column(String(10), nullable=False, default="month")  # day | week | month | year
    max_devices = Column(Integer, nullable=True)  # None = unlimited
    description = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def duration_days(self) -> int:
        """Normalise the configured duration to days (for expiry calculation)."""
        unit_days = {"day": 1, "week": 7, "month": 30, "year": 365}
        return self.duration_value * unit_days.get(self.duration_unit, 30)

    def __repr__(self):
        return f"<SubscriptionPlan(slug='{self.slug}', {self.price} {self.currency}/{self.duration_value}{self.duration_unit})>"


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    clerk_user_id = Column(String(255), nullable=False, index=True)
    # Real FK as of migrations 041/042 — was a bare slug string (see
    # `plan_slug_legacy`, kept post-migration for audit only). Every
    # client-facing response still serializes the plan's .slug via this
    # relationship, never the raw id — see app/services/plan_resolution.py.
    plan_id = Column(Integer, ForeignKey("subscription_plans.id"), nullable=False, index=True)
    plan = relationship("SubscriptionPlan", foreign_keys=[plan_id])
    status = Column(String(20), default="active")
    price = Column(Float, nullable=False, default=0.0)  # Stored at purchase time so admin edits don't affect existing subscribers
    started_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    # Set once the "expiring soon" push+email has been sent (see
    # scripts/cron_expiry.py's notify_expiring_subscriptions) so the
    # reminder fires exactly once per subscription, not on every cron run.
    expiry_reminder_sent_at = Column(DateTime, nullable=True)
    # Vehicle slots paid for (migration 048) — every plan is priced per
    # vehicle, and a subscription covers at most this many vehicles (see
    # SubscriptionVehicle). A vehicle can take a free slot at no charge;
    # more vehicles than slots means buying extra slots, prorated.
    quantity = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    vehicles = relationship("SubscriptionVehicle", back_populates="subscription",
                            cascade="all, delete-orphan", passive_deletes=True)


class SubscriptionVehicle(Base):
    """One vehicle a subscription covers (migration 048). Uncovered vehicles
    stay on the account but their premium features are refused — see
    app/services/entitlements.py's vehicle_not_covered reason."""
    __tablename__ = "subscription_vehicles"
    __table_args__ = (UniqueConstraint("subscription_id", "vehicle_id", name="uq_subscription_vehicles_pair"),)

    id = Column(Integer, primary_key=True, index=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False, index=True)
    # The payment that bought this vehicle's slot, if any (NULL for a free
    # slot, the trial, an admin assignment, or the migration backfill).
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=True)
    added_at = Column(DateTime, default=datetime.utcnow)

    subscription = relationship("Subscription", back_populates="vehicles")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    clerk_user_id = Column(String(255), nullable=False, index=True)
    tx_ref = Column(String(255), nullable=False, unique=True, index=True)
    # Real FK as of migrations 041/042 — see Subscription.plan_id's doc above.
    plan_id = Column(Integer, ForeignKey("subscription_plans.id"), nullable=False, index=True)
    plan = relationship("SubscriptionPlan", foreign_keys=[plan_id])
    amount = Column(Float, nullable=False)
    currency = Column(String(10), default="RWF")
    status = Column(String(20), nullable=False)
    verified_at = Column(DateTime, default=datetime.utcnow)
    # Set once this payment has activated a subscription (migration 046) —
    # a consumed payment can never fund another one. Claimed atomically by
    # app/api/onboarding.py's _claim_payment.
    consumed_at = Column(DateTime, nullable=True)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=True)
    # What the payment is for (migration 048): "subscribe" (a new
    # subscription, incl. switching plans) or "add_vehicles" (extra slots
    # on the current subscription, prorated), and exactly which vehicles —
    # fixed at payment time so activation can't cover more than was paid.
    purpose = Column(String(20), nullable=False, default="subscribe")
    vehicle_ids = Column(JSON, nullable=True)

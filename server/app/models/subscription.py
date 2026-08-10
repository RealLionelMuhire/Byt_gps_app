from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean
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
    plan_id = Column(String(20), nullable=False)
    status = Column(String(20), default="active")
    price = Column(Float, nullable=False, default=0.0)  # Stored at purchase time so admin edits don't affect existing subscribers
    started_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    clerk_user_id = Column(String(255), nullable=False, index=True)
    tx_ref = Column(String(255), nullable=False, unique=True, index=True)
    plan_id = Column(String(20), nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), default="RWF")
    status = Column(String(20), nullable=False)
    verified_at = Column(DateTime, default=datetime.utcnow)

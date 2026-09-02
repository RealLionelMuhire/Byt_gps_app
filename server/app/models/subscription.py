from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.core.database import Base


class SubscriptionPlan(Base):
    """
    Admin-configurable subscription plan.

    - billing_type: 'prepaid' (pay now, use for duration)
                    | 'postpaid' (use now, pay later / monthly bill)
    - pricing_model: 'per_device' (price × number of devices)
                     | 'flat' (single price covers all devices)
    - price + currency: e.g. 5000 RWF per duration
    - duration_value + duration_unit: e.g. 1 month, 14 days, 1 year
    - min_devices: minimum devices required to choose this plan
    - max_devices: maximum devices allowed (None = unlimited)

    The `slug` is the identifier the mobile app sends as `planId`
    (trial / basic / fleet by default).
    """
    __tablename__ = "subscription_plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    slug = Column(String(50), unique=True, nullable=False, index=True)
    billing_type = Column(String(20), nullable=False, default="prepaid")  # prepaid | postpaid
    pricing_model = Column(String(20), nullable=False, default="flat")  # per_device | flat
    price = Column(Float, nullable=False, default=0.0)
    currency = Column(String(10), nullable=False, default="RWF")
    duration_value = Column(Integer, nullable=False, default=1)
    duration_unit = Column(String(10), nullable=False, default="month")  # day | week | month | year
    min_devices = Column(Integer, nullable=False, default=1)  # minimum devices to choose this plan
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
    """
    A company's subscription to a plan.

    - company_id: the company this subscription belongs to
    - clerk_user_id: the user who created/subscribed (from JWT, legacy compat)
    - plan_id: slug of the plan chosen
    - billing_type: 'prepaid' | 'postpaid' (snapshot from plan at subscription time)
    - pricing_model: 'per_device' | 'flat' (snapshot from plan at subscription time)
    - device_count_snapshot: number of devices at subscription time (for per_device billing)
    - price: unit price from plan
    - amount_due: total to pay (price × device_count if per_device, else just price)
    - payment_status: 'pending' | 'paid' | 'overdue' | 'partial'
    - due_date: when payment is due (for postpaid: end of billing period; prepaid: null)
    - started_at: when the subscription started
    - expires_at: calculated from started_at + plan.duration (NOT user input)
    - status: active → expired → cancelled
    """
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)
    clerk_user_id = Column(String(255), nullable=True, index=True)  # who subscribed (legacy/user-level)
    plan_id = Column(String(20), nullable=False)  # slug of the plan
    billing_type = Column(String(20), nullable=False, default="prepaid")  # prepaid | postpaid (snapshot)
    pricing_model = Column(String(20), nullable=False, default="flat")  # per_device | flat (snapshot)
    device_count_snapshot = Column(Integer, nullable=False, default=0)  # devices at subscription time
    status = Column(String(20), default="active")  # active → expired → cancelled
    price = Column(Float, nullable=False, default=0.0)  # unit price from plan
    amount_due = Column(Float, nullable=False, default=0.0)  # total: price × devices or flat price
    payment_status = Column(String(20), default="pending")  # pending | paid | overdue | partial
    due_date = Column(DateTime, nullable=True)  # when payment is due (postpaid only)
    started_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)  # calculated: started_at + plan.duration
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", back_populates="subscriptions")


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

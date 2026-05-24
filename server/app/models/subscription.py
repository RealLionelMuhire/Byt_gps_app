from sqlalchemy import Column, Integer, String, DateTime, Float
from datetime import datetime
from app.core.database import Base

class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    clerk_user_id = Column(String(255), nullable=False, index=True)
    plan_id = Column(String(20), nullable=False)
    status = Column(String(20), default="active")
    started_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


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

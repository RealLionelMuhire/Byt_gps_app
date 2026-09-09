from sqlalchemy import Column, Integer, String, Text, DateTime
from datetime import datetime
from app.core.database import Base


class ContactRequest(Base):
    """A contact request raised from the mobile app (order a GPS device,
    or support), visible to admins via GET /api/contact-requests."""
    __tablename__ = "contact_requests"

    id = Column(Integer, primary_key=True, index=True)
    clerk_user_id = Column(String(255), nullable=False, index=True)
    type = Column(String(20), nullable=False)
    name = Column(String(255), nullable=False)
    phone = Column(String(32), nullable=False)
    message = Column(Text, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

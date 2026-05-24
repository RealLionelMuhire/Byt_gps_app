"""User model"""

from sqlalchemy import Column, Integer, String, DateTime, Boolean
from datetime import datetime

from app.core.database import Base


class User(Base):
    """User account - synced from Clerk authentication"""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    clerk_user_id = Column(String(255), unique=True, nullable=False, index=True)
    email = Column(String(255), nullable=False, index=True)
    first_name = Column(String(255), nullable=False)
    last_name = Column(String(255), nullable=False)
    role = Column(String(20), default="owner", nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    onboarding_step = Column(Integer, default=0, nullable=False)
    onboarding_complete = Column(Boolean, default=False, nullable=False)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f"<User(clerk_user_id='{self.clerk_user_id}', email='{self.email}')>"

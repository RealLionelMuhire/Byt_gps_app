"""User model"""

from sqlalchemy import Column, Integer, String, DateTime
from datetime import datetime

from app.core.database import Base


class User(Base):
    """User account - synced from Clerk authentication"""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    clerk_user_id = Column(String(255), unique=True, nullable=False, index=True)
    email = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f"<User(clerk_user_id='{self.clerk_user_id}', email='{self.email}')>"

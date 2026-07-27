"""User model"""

import enum
from sqlalchemy import Column, Integer, String, DateTime, Boolean
from sqlalchemy import Enum as SAEnum
from datetime import datetime

from app.core.database import Base


class Role(str, enum.Enum):
    """User roles for role-based access control."""
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    TECHNICIAN = "TECHNICIAN"
    USER = "USER"


class User(Base):
    """User account — authenticated via custom JWT (no third-party provider)."""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    # clerk_user_id kept for DB backwards-compat; now holds the user's email
    # (or original Clerk ID for existing rows). New registrations set it to email.
    clerk_user_id = Column(String(255), unique=True, nullable=False, index=True)
    email = Column(String(255), nullable=False, index=True)
    first_name = Column(String(255), nullable=False)
    last_name = Column(String(255), nullable=False)
    # Password hash (bcrypt). Nullable for rows migrated from Clerk (they must reset).
    password_hash = Column(String(255), nullable=True)
    role = Column(SAEnum(Role, name="user_role", create_type=False), default=Role.USER, nullable=False)
    onboarding_step = Column(Integer, default=0, nullable=False)
    onboarding_complete = Column(Boolean, default=False, nullable=False)
    expo_push_token = Column(String(255), nullable=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    @property
    def is_admin(self) -> bool:
        return self.role in (Role.SUPER_ADMIN, Role.ADMIN)
    
    def __repr__(self):
        return f"<User(email='{self.email}', role='{self.role.value}')>"

"""Company and Membership models"""

import enum
from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, Enum as SAEnum, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime

from app.core.database import Base


class CompanyRole(str, enum.Enum):
    """Role within a company — separate from the global User.role (SUPER_ADMIN/ADMIN/TECHNICIAN/USER)."""
    OWNER = "OWNER"
    USER = "USER"


class Company(Base):
    """
    A company (or solo-user workspace).

    - is_company=False: solo user — name is derived from the user's first/last
      name at creation time. No explicit company name was provided.
    - is_company=True: named company — the user provided a company name during
      onboarding.

    Every user gets exactly one Company row at onboarding step 5. The company
    is automatically created (name defaults from user's name if not supplied).
    """
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    is_company = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    memberships = relationship("Membership", back_populates="company", cascade="all, delete-orphan")
    invite_codes = relationship("InviteCode", cascade="all, delete-orphan")
    devices = relationship("Device", back_populates="company")
    subscriptions = relationship("Subscription", back_populates="company")

    def __repr__(self):
        return f"<Company(id={self.id}, name='{self.name}', is_company={self.is_company})>"


class Membership(Base):
    """
    Links a User to a Company with a role.

    - OWNER: created automatically when the user creates their company during
      onboarding. Can manage company settings, invite/remove members, etc.
    - USER: a team member invited by an OWNER. Can view/manage devices the
      OWNER grants access to.

    A user has exactly one Membership per Company. A user can belong to multiple
    companies in the future (for now, onboarding creates exactly one).
    """
    __tablename__ = "memberships"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    company_role = Column(SAEnum(CompanyRole, name="company_role", create_type=False), nullable=False, default=CompanyRole.USER)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    user = relationship("User", backref="memberships")
    company = relationship("Company", back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("user_id", "company_id", name="uq_user_company"),
    )

    def __repr__(self):
        return f"<Membership(user_id={self.user_id}, company_id={self.company_id}, role='{self.company_role.value}')>"


class InviteCode(Base):
    """
    A single-use invite code for joining a company.

    Created by an OWNER or ADMIN of the company. A user who calls
    POST /companies/{id}/join with a valid, unused code is added as a
    member with the role specified at creation time.
    """
    __tablename__ = "company_invite_codes"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    code = Column(String(32), nullable=False, unique=True)
    role = Column(SAEnum(CompanyRole, name="company_role", create_type=False), nullable=False, default=CompanyRole.USER)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    used_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    company = relationship("Company")
    creator = relationship("User", foreign_keys=[created_by])
    user = relationship("User", foreign_keys=[used_by])

    def __repr__(self):
        return f"<InviteCode(id={self.id}, company_id={self.company_id}, code='{self.code}', role='{self.role.value}')>"

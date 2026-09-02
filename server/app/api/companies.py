"""
Company API — onboarding step 5.

POST /api/companies
    Create a company (and OWNER membership) for the authenticated user.
    - If `name` is provided → is_company=True, company name = provided name.
    - If `name` is omitted  → is_company=False, company name = ".FirstName LastName".
    The user's onboarding_step is bumped to 5 on success.

This is the only company-creation path for now. Admin-created companies
(inviting users to an existing company) will be a future endpoint.
"""

import logging
import secrets
import string
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime

from typing import List
from sqlalchemy import or_, func

from app.core.database import get_db
from app.core.auth import require_auth, get_current_user, require_admin
from app.models.user import User
from app.models.company import Company, Membership, CompanyRole, InviteCode

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class CompanyCreateRequest(BaseModel):
    """Request body for POST /api/companies.

    `name` is optional:
      - Provided  → is_company=True, company name = provided name.
      - Omitted   → is_company=False, company name derived from user's name.
    """
    name: Optional[str] = None


class CompanyResponse(BaseModel):
    companyId: int
    name: str
    isCompany: bool
    membershipId: int
    companyRole: str

    class Config:
        from_attributes = True


# ── Endpoint: POST /api/companies (Step 5) ────────────────────────────────────

@router.post("/companies", response_model=CompanyResponse, status_code=201)
async def create_company(
    body: CompanyCreateRequest,
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """
    Create a company and OWNER membership for the authenticated user.

    This is onboarding step 5 — called after user profile creation (step 4)
    and before device pairing (step 6).

    - One company per user for now (idempotent: returns existing if already created).
    - Bumps user.onboarding_step to 5 on success.
    """
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found. Complete profile step first.")

    # Idempotent: if user already has a company + membership, return it
    existing_membership = (
        db.query(Membership)
        .filter(Membership.user_id == user.id)
        .order_by(Membership.created_at.asc())
        .first()
    )
    if existing_membership:
        company = db.query(Company).filter(Company.id == existing_membership.company_id).first()
        logger.info("Company already exists for user %s — returning existing (id=%d)", clerk_user_id, company.id)
        return CompanyResponse(
            companyId=company.id,
            name=company.name,
            isCompany=company.is_company,
            membershipId=existing_membership.id,
            companyRole=existing_membership.company_role.value,
        )

    # Determine company name and is_company flag
    provided_name = (body.name or "").strip()
    if provided_name:
        company_name = provided_name
        is_company = True
    else:
        # Derive from user's name
        first = (user.first_name or "").strip()
        last = (user.last_name or "").strip()
        company_name = f"{first} {last}".strip() or user.email
        is_company = False

    try:
        # Create company
        company = Company(
            name=company_name,
            is_company=is_company,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(company)
        db.flush()  # Get company.id

        # Create OWNER membership
        membership = Membership(
            user_id=user.id,
            company_id=company.id,
            company_role=CompanyRole.OWNER,
            created_at=datetime.utcnow(),
        )
        db.add(membership)

        # Bump onboarding step
        user.onboarding_step = 5
        user.updated_at = datetime.utcnow()

        db.commit()
        db.refresh(company)
        db.refresh(membership)

        logger.info(
            "Company created: id=%d name='%s' is_company=%s for user %s (membership_id=%d)",
            company.id, company.name, company.is_company, clerk_user_id, membership.id,
        )

        return CompanyResponse(
            companyId=company.id,
            name=company.name,
            isCompany=company.is_company,
            membershipId=membership.id,
            companyRole=membership.company_role.value,
        )

    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error creating company for user %s: %s", clerk_user_id, exc)
        raise HTTPException(status_code=500, detail="Database error")


# ── Endpoint: POST /api/admin/companies (admin creates company) ────────────────

class AdminCompanyCreateRequest(BaseModel):
    """Body for admin-created company. Name is required."""
    name: str
    is_company: bool = True


class AdminCompanyResponse(BaseModel):
    companyId: int
    name: str
    isCompany: bool
    createdAt: str

    class Config:
        from_attributes = True


@router.post("/admin/companies", response_model=AdminCompanyResponse, status_code=201)
async def admin_create_company(
    body: AdminCompanyCreateRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Create a company as a global admin. The admin is NOT added as a member.

    Use this to pre-create companies that users will later join via invite
    code or be added to by the admin.
    """
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Company name is required.")

    try:
        company = Company(
            name=name,
            is_company=body.is_company,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(company)
        db.commit()
        db.refresh(company)

        logger.info(
            "Admin %d created company %d ('%s') — no membership created",
            user.id, company.id, company.name,
        )

        return AdminCompanyResponse(
            companyId=company.id,
            name=company.name,
            isCompany=company.is_company,
            createdAt=company.created_at.isoformat(),
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error creating company: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


# ── Endpoint: GET /api/companies/search ────────────────────────────────────────

class CompanySearchResult(BaseModel):
    companyId: int
    name: str
    isCompany: bool
    memberCount: int
    createdAt: str

    class Config:
        from_attributes = True


@router.get("/companies/search", response_model=List[CompanySearchResult])
async def search_companies(
    q: str = "",
    limit: int = 20,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """
    Search companies by name or by member name/email.

    - Matches company name (partial, case-insensitive)
    - OR matches any member's first_name, last_name, or email (partial, case-insensitive)
    - Returns deduplicated companies with member count.
    - Authenticated users only (any role).
    """
    if not q or not q.strip():
        return []

    pattern = f"%{q.strip()}%"

    # Find company IDs matching by company name
    name_match_ids = [
        row[0] for row in
        db.query(Company.id).filter(Company.name.ilike(pattern)).limit(100).all()
    ]

    # Find company IDs where a member's name or email matches
    member_match_ids = [
        row[0] for row in
        db.query(Membership.company_id)
        .join(User, Membership.user_id == User.id)
        .filter(
            or_(
                User.first_name.ilike(pattern),
                User.last_name.ilike(pattern),
                User.email.ilike(pattern),
                func.concat(User.first_name, " ", User.last_name).ilike(pattern),
            )
        )
        .distinct()
        .limit(100)
        .all()
    ]

    # Deduplicate
    company_ids = list(set(name_match_ids + member_match_ids))
    if not company_ids:
        return []

    # Fetch companies
    companies = db.query(Company).filter(Company.id.in_(company_ids)).limit(limit).all()

    # Batch-load member counts
    member_counts = dict(
        db.query(Membership.company_id, func.count(Membership.id))
        .filter(Membership.company_id.in_(company_ids))
        .group_by(Membership.company_id)
        .all()
    )

    return [
        CompanySearchResult(
            companyId=c.id,
            name=c.name,
            isCompany=c.is_company,
            memberCount=member_counts.get(c.id, 0),
            createdAt=c.created_at.isoformat(),
        )
        for c in companies
    ]


# ── Helper: check membership ──────────────────────────────────────────────────

def _require_membership(user_id: int, company_id: int, db: Session) -> Membership:
    """Return the membership for (user_id, company_id) or raise 404."""
    membership = (
        db.query(Membership)
        .filter(Membership.user_id == user_id, Membership.company_id == company_id)
        .first()
    )
    if not membership:
        raise HTTPException(status_code=404, detail="You are not a member of this company.")
    return membership


def _require_company_owner_or_admin(user_id: int, company_id: int, db: Session) -> Membership:
    """Return the membership if user is OWNER or has ADMIN/TECHNICIAN global role, else 403."""
    membership = _require_membership(user_id, company_id, db)
    user = db.query(User).filter(User.id == user_id).first()
    if membership.company_role != CompanyRole.OWNER and user.role not in ("SUPER_ADMIN", "ADMIN"):
        raise HTTPException(status_code=403, detail="Only company owners or admins can perform this action.")
    return membership


def _generate_invite_code() -> str:
    """Generate a short alphanumeric invite code."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


# ── Endpoint: POST /companies/{company_id}/invite-codes ────────────────────────

class InviteCodeCreateRequest(BaseModel):
    role: str = "USER"
    expires_in_hours: Optional[int] = None  # optional TTL


class InviteCodeResponse(BaseModel):
    id: int
    code: str
    role: str
    companyId: int
    createdBy: int
    expiresAt: Optional[str] = None
    createdAt: str

    class Config:
        from_attributes = True


@router.post("/companies/{company_id}/invite-codes", response_model=InviteCodeResponse, status_code=201)
async def create_invite_code(
    company_id: int,
    body: InviteCodeCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create a single-use invite code for joining a company.

    Only the company OWNER or an ADMIN/TECHNICIAN (global role) can create invite codes.
    The code is valid for one use. Optionally specify a role and expiry.
    """
    _require_company_owner_or_admin(user.id, company_id, db)

    # Validate role
    try:
        company_role = CompanyRole(body.role.upper())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid role '{body.role}'. Must be one of: OWNER, USER")

    # Prevent inviting as OWNER
    if company_role == CompanyRole.OWNER:
        raise HTTPException(status_code=400, detail="Cannot invite as OWNER. Use USER role.")

    code = _generate_invite_code()
    expires_at = None
    if body.expires_in_hours and body.expires_in_hours > 0:
        from datetime import timedelta
        expires_at = datetime.utcnow() + timedelta(hours=body.expires_in_hours)

    try:
        invite = InviteCode(
            company_id=company_id,
            code=code,
            role=company_role,
            created_by=user.id,
            expires_at=expires_at,
            created_at=datetime.utcnow(),
        )
        db.add(invite)
        db.commit()
        db.refresh(invite)

        logger.info("Invite code created: %s for company %d by user %d (role=%s)", code, company_id, user.id, company_role.value)

        return InviteCodeResponse(
            id=invite.id,
            code=invite.code,
            role=invite.role.value,
            companyId=invite.company_id,
            createdBy=invite.created_by,
            expiresAt=invite.expires_at.isoformat() if invite.expires_at else None,
            createdAt=invite.created_at.isoformat(),
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error creating invite code: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


# ── Endpoint: POST /companies/{company_id}/join ────────────────────────────────

class JoinCompanyRequest(BaseModel):
    code: str


class JoinCompanyResponse(BaseModel):
    membershipId: int
    companyId: int
    companyRole: str
    companyName: str

    class Config:
        from_attributes = True


@router.post("/companies/{company_id}/join", response_model=JoinCompanyResponse, status_code=201)
async def join_company(
    company_id: int,
    body: JoinCompanyRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Join a company using a valid invite code.

    - Code must exist for this company, not be expired, and not yet used.
    - The user must not already be a member of this company.
    - On success, a Membership is created with the role specified when the
      invite code was generated.
    """
    # Check company exists
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    # Check user isn't already a member
    existing = (
        db.query(Membership)
        .filter(Membership.user_id == user.id, Membership.company_id == company_id)
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="You are already a member of this company.")

    # Find the invite code
    invite = (
        db.query(InviteCode)
        .filter(InviteCode.company_id == company_id, InviteCode.code == body.code)
        .first()
    )
    if not invite:
        raise HTTPException(status_code=404, detail="Invalid invite code.")

    # Check not already used
    if invite.used_by is not None:
        raise HTTPException(status_code=400, detail="This invite code has already been used.")

    # Check not expired
    if invite.expires_at and invite.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="This invite code has expired.")

    try:
        # Mark invite as used
        invite.used_by = user.id
        invite.used_at = datetime.utcnow()

        # Create membership
        membership = Membership(
            user_id=user.id,
            company_id=company_id,
            company_role=invite.role,
            created_at=datetime.utcnow(),
        )
        db.add(membership)
        db.commit()
        db.refresh(membership)

        logger.info(
            "User %d joined company %d via invite code %s (role=%s)",
            user.id, company_id, invite.code, invite.role.value,
        )

        return JoinCompanyResponse(
            membershipId=membership.id,
            companyId=company_id,
            companyRole=membership.company_role.value,
            companyName=company.name,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error joining company: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


# ── Endpoint: POST /companies/{company_id}/members (admin add) ─────────────────

class AddMemberRequest(BaseModel):
    user_id: int
    role: str = "USER"


class AddMemberResponse(BaseModel):
    membershipId: int
    companyId: int
    userId: int
    companyRole: str

    class Config:
        from_attributes = True


@router.post("/companies/{company_id}/members", response_model=AddMemberResponse, status_code=201)
async def add_member(
    company_id: int,
    body: AddMemberRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Add a user directly to a company. Requires ADMIN or SUPER_ADMIN global role.

    The target user must exist and not already be a member of this company.
    """
    # Check company exists
    company = db.query(Company).filter(Company.id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")

    # Check target user exists
    target_user = db.query(User).filter(User.id == body.user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="Target user not found.")

    # Check target user isn't already a member
    existing = (
        db.query(Membership)
        .filter(Membership.user_id == body.user_id, Membership.company_id == company_id)
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="User is already a member of this company.")

    # Validate role
    try:
        company_role = CompanyRole(body.role.upper())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid role '{body.role}'. Must be one of: OWNER, USER")

    if company_role == CompanyRole.OWNER:
        raise HTTPException(status_code=400, detail="Cannot assign OWNER via this endpoint.")

    try:
        membership = Membership(
            user_id=body.user_id,
            company_id=company_id,
            company_role=company_role,
            created_at=datetime.utcnow(),
        )
        db.add(membership)
        db.commit()
        db.refresh(membership)

        logger.info(
            "Admin %d added user %d to company %d (role=%s)",
            user.id, body.user_id, company_id, company_role.value,
        )

        return AddMemberResponse(
            membershipId=membership.id,
            companyId=company_id,
            userId=body.user_id,
            companyRole=membership.company_role.value,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error adding member: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


# ── Endpoint: DELETE /companies/{company_id}/members/{user_id} ─────────────────

class RemoveMemberResponse(BaseModel):
    message: str
    companyId: int
    userId: int


@router.delete("/companies/{company_id}/members/{user_id}", response_model=RemoveMemberResponse)
async def remove_member(
    company_id: int,
    user_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Remove a member from a company.

    Only the company OWNER or a global ADMIN/SUPER_ADMIN can remove members.
    Cannot remove yourself (the OWNER) from the company.
    """
    _require_company_owner_or_admin(user.id, company_id, db)

    # Check target membership exists
    target_membership = (
        db.query(Membership)
        .filter(Membership.user_id == user_id, Membership.company_id == company_id)
        .first()
    )
    if not target_membership:
        raise HTTPException(status_code=404, detail="User is not a member of this company.")

    # Cannot remove yourself
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot remove yourself from the company.")

    # Cannot remove the OWNER
    if target_membership.company_role == CompanyRole.OWNER:
        raise HTTPException(status_code=400, detail="Cannot remove the company owner.")

    try:
        db.delete(target_membership)
        db.commit()

        logger.info(
            "User %d removed user %d from company %d",
            user.id, user_id, company_id,
        )

        return RemoveMemberResponse(
            message="Member removed successfully.",
            companyId=company_id,
            userId=user_id,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error removing member: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")


# ── Endpoint: PUT /companies/{company_id}/members/{user_id}/role ───────────────

class ChangeRoleRequest(BaseModel):
    role: str


class ChangeRoleResponse(BaseModel):
    membershipId: int
    companyId: int
    userId: int
    companyRole: str

    class Config:
        from_attributes = True


@router.put("/companies/{company_id}/members/{user_id}/role", response_model=ChangeRoleResponse)
async def change_member_role(
    company_id: int,
    user_id: int,
    body: ChangeRoleRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Change a member's role within a company.

    Only the company OWNER or a global ADMIN/SUPER_ADMIN can change roles.
    Cannot change the OWNER's role.
    """
    _require_company_owner_or_admin(user.id, company_id, db)

    # Check target membership exists
    target_membership = (
        db.query(Membership)
        .filter(Membership.user_id == user_id, Membership.company_id == company_id)
        .first()
    )
    if not target_membership:
        raise HTTPException(status_code=404, detail="User is not a member of this company.")

    # Cannot change the OWNER's role
    if target_membership.company_role == CompanyRole.OWNER:
        raise HTTPException(status_code=400, detail="Cannot change the company owner's role.")

    # Validate role
    try:
        new_role = CompanyRole(body.role.upper())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid role '{body.role}'. Must be one of: OWNER, USER")

    if new_role == CompanyRole.OWNER:
        raise HTTPException(status_code=400, detail="Cannot assign OWNER via this endpoint.")

    # No-op if same role
    if target_membership.company_role == new_role:
        return ChangeRoleResponse(
            membershipId=target_membership.id,
            companyId=company_id,
            userId=user_id,
            companyRole=target_membership.company_role.value,
        )

    try:
        target_membership.company_role = new_role
        db.commit()
        db.refresh(target_membership)

        logger.info(
            "User %d changed user %d role in company %d to %s",
            user.id, user_id, company_id, new_role.value,
        )

        return ChangeRoleResponse(
            membershipId=target_membership.id,
            companyId=company_id,
            userId=user_id,
            companyRole=target_membership.company_role.value,
        )
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("DB error changing member role: %s", exc)
        raise HTTPException(status_code=500, detail="Database error")

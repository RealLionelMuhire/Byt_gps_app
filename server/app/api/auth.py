"""
Authentication API endpoints — custom JWT auth (no third-party provider).

Endpoints:
  POST /api/auth/register   — Create new account, return JWT
  POST /api/auth/login      — Verify credentials, return JWT
  GET  /api/auth/me         — Return current user profile
  POST /api/auth/sync       — Upsert user record (called from onboarding)
  PUT  /api/auth/push-token — Save Expo push notification token
  GET  /api/auth/users      — List all users (admin only)
  PUT  /api/auth/users/{id}/role — Update user role (admin only)
"""

from typing import Optional, List
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from jose import jwt
import logging

from app.core.database import get_db
from app.core.config import settings
from app.core.auth import require_auth, require_admin
from app.models.user import User, Role

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Password hashing ──────────────────────────────────────────────────────────

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def _verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


# ── JWT helpers ───────────────────────────────────────────────────────────────

def _create_token(subject: str) -> str:
    """Create a signed HS256 JWT with sub=subject and an expiry."""
    expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": subject, "exp": expire},
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "email": "user@example.com",
                "password": "StrongPass1!",
                "first_name": "John",
                "last_name": "Doe",
            }
        }


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    clerk_user_id: str
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    role: str
    onboarding_step: Optional[int] = 0
    onboarding_complete: Optional[bool] = False
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class UserSyncRequest(BaseModel):
    """Request body for user sync from the mobile onboarding flow."""
    clerk_user_id: str        # kept for API backwards-compat; treated as a stable user ID
    email: EmailStr
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    name: Optional[str] = None  # full name alternative; split into first/last

    class Config:
        json_schema_extra = {
            "example": {
                "clerk_user_id": "user@example.com",
                "email": "user@example.com",
                "name": "John Doe",
            }
        }


# ── Auth endpoints ────────────────────────────────────────────────────────────

@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(
    body: RegisterRequest,
    db: Session = Depends(get_db),
):
    """
    Register a new user and return a JWT.

    - **email**: User email address
    - **password**: Plain-text password (≥ 8 chars, stored as bcrypt hash)
    - **first_name / last_name**: Optional name fields
    """
    # Check for duplicate email
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    if len(body.password) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Password must be at least 8 characters.",
        )

    is_first = db.query(User).count() == 0
    initial_role = Role.SUPER_ADMIN if is_first else Role.USER

    user = User(
        clerk_user_id=body.email,  # use email as stable identifier
        email=body.email,
        first_name=body.first_name or "Unknown",
        last_name=body.last_name or "Unknown",
        password_hash=_hash_password(body.password),
        role=initial_role,
        onboarding_complete=is_first,
        onboarding_step=9 if is_first else 0,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    try:
        db.add(user)
        db.commit()
        db.refresh(user)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("register: DB error — %s", exc)
        raise HTTPException(status_code=500, detail="Database error during registration.")

    token = _create_token(body.email)
    logger.info("New user registered: %s (role=%s)", body.email, initial_role.value)
    return {"access_token": token, "token_type": "bearer", "user": user}


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    db: Session = Depends(get_db),
):
    """
    Authenticate with email + password and return a JWT.
    """
    user = db.query(User).filter(User.email == body.email).first()

    if not user or not user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    if not _verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    token = _create_token(body.email)
    logger.info("User logged in: %s", body.email)
    return {"access_token": token, "token_type": "bearer", "user": user}


@router.get("/me", response_model=UserResponse)
async def get_current_user(
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Return the current authenticated user's profile."""
    user = (
        db.query(User).filter(User.email == user_id).first()
        or db.query(User).filter(User.clerk_user_id == user_id).first()
    )
    if not user:
        raise HTTPException(
            status_code=401,
            detail="User profile not found. Register via POST /api/auth/register first.",
        )
    return user


@router.post("/sync", response_model=UserResponse, status_code=200)
async def sync_user(
    user_data: UserSyncRequest,
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """
    Upsert user record from the mobile onboarding flow.
    Called by the app after sign-up to ensure the backend DB is up to date.
    """
    try:
        # Resolve first/last name
        resolved_first = user_data.first_name
        resolved_last = user_data.last_name
        if not resolved_first and not resolved_last and user_data.name:
            parts = user_data.name.strip().split(" ", 1)
            resolved_first = parts[0]
            resolved_last = parts[1] if len(parts) > 1 else ""

        # Try by email first, then clerk_user_id for backwards compat
        user = (
            db.query(User).filter(User.email == user_data.email).first()
            or db.query(User).filter(User.clerk_user_id == user_data.clerk_user_id).first()
        )

        if user:
            user.email = user_data.email
            if resolved_first:
                user.first_name = resolved_first
            if resolved_last:
                user.last_name = resolved_last
            user.updated_at = datetime.utcnow()
        else:
            is_first = db.query(User).count() == 0
            initial_role = Role.SUPER_ADMIN if is_first else Role.USER
            user = User(
                clerk_user_id=user_data.clerk_user_id or user_data.email,
                email=user_data.email,
                first_name=resolved_first or "Unknown",
                last_name=resolved_last or "Unknown",
                role=initial_role,
                onboarding_complete=is_first,
                onboarding_step=9 if is_first else 0,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(user)

        if user.is_admin and not user.onboarding_complete:
            user.onboarding_complete = True
            user.onboarding_step = 9

        db.commit()
        db.refresh(user)
        return user

    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("sync_user: DB error — %s", exc)
        raise HTTPException(status_code=500, detail="Database error occurred")
    except Exception as exc:
        db.rollback()
        logger.error("sync_user: unexpected error — %s", exc)
        raise HTTPException(status_code=500, detail="An unexpected error occurred")


@router.get("/user/{user_email}", response_model=UserResponse)
async def get_user_by_email(
    user_email: str,
    db: Session = Depends(get_db),
):
    """Get user by email."""
    user = db.query(User).filter(User.email == user_email).first()
    if not user:
        raise HTTPException(status_code=404, detail=f"User not found: {user_email}")
    return user


# ── Push notification token ────────────────────────────────────────────────

class PushTokenRequest(BaseModel):
    token: str


@router.put("/push-token", status_code=200)
async def update_push_token(
    body: PushTokenRequest,
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Store (or update) the Expo push token for the authenticated user."""
    user = (
        db.query(User).filter(User.email == user_id).first()
        or db.query(User).filter(User.clerk_user_id == user_id).first()
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.expo_push_token = body.token
    user.updated_at = datetime.utcnow()
    db.commit()
    logger.info("Push token updated for user %s", user_id)
    return {"ok": True}


# ── User Management (Admin only) ───────────────────────────────────────────

class UpdateRoleRequest(BaseModel):
    role: str

    class Config:
        json_schema_extra = {"example": {"role": "ADMIN"}}


class UserListResponse(BaseModel):
    id: int
    clerk_user_id: str
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    role: str
    onboarding_complete: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


@router.get("/users", response_model=List[UserListResponse])
async def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """List all users. Requires ADMIN or SUPER_ADMIN role."""
    return db.query(User).order_by(User.created_at.asc()).offset(skip).limit(limit).all()


@router.put("/users/{user_id}/role", response_model=UserResponse)
async def update_user_role(
    user_id: int,
    body: UpdateRoleRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Update a user's role. Requires ADMIN or SUPER_ADMIN."""
    try:
        new_role = Role(body.role.upper())
    except ValueError:
        valid = [r.value for r in Role]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role '{body.role}'. Must be one of: {', '.join(valid)}",
        )

    if new_role == Role.SUPER_ADMIN and current_user.role != Role.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Only SUPER_ADMIN can assign the SUPER_ADMIN role.")

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target.id == current_user.id and new_role != Role.SUPER_ADMIN and current_user.role == Role.SUPER_ADMIN:
        count = db.query(User).filter(User.role == Role.SUPER_ADMIN).count()
        if count <= 1:
            raise HTTPException(
                status_code=409,
                detail="Cannot demote the last SUPER_ADMIN. Promote another user first.",
            )

    if target.role == Role.SUPER_ADMIN and current_user.role != Role.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Only SUPER_ADMIN can change another SUPER_ADMIN's role.")

    old_role = target.role.value
    target.role = new_role
    target.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(target)
    logger.info("User %d role changed %s → %s by user %d", target.id, old_role, new_role.value, current_user.id)
    return target

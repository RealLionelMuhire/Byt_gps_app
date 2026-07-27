"""
Shared authentication dependency for FastAPI routes.

Reads the `Authorization: Bearer <token>` header and validates the
JWT using HS256 with the server's local SECRET_KEY. Routes that depend
on `require_auth` will return HTTP 401 if the token is missing or invalid.

Usage:
    from app.core.auth import require_auth, require_admin, require_technician

    @router.get("/something")
    async def my_route(user_id: str = Depends(require_auth)):
        ...  # user_id is the verified user identifier (email) from the JWT

Role-based access:
    @router.get("/admin-only")
    async def admin_route(user: User = Depends(require_admin)):
        ...

    @router.get("/tech-or-admin")
    async def tech_route(user: User = Depends(require_technician)):
        ...

Dev/offline mode:
    If SECRET_KEY is the default placeholder the token is still verified
    (unlike Clerk dev mode), so set a real SECRET_KEY in production.
"""

import logging
from typing import Optional

from jose import jwt, JWTError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.user import User, Role

logger = logging.getLogger(__name__)

# Reusable FastAPI security scheme — extracts the Bearer token from the header
_bearer = HTTPBearer(auto_error=False)


def _decode_token(token: str) -> Optional[str]:
    """
    Decode and verify a JWT issued by this server.

    Returns the `sub` claim (user identifier — email) on success,
    or None on failure.
    """
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            options={"verify_aud": False},
        )
        sub: Optional[str] = payload.get("sub")
        if not sub:
            logger.warning("AUTH: JWT has no 'sub' claim")
            return None
        return sub
    except JWTError as exc:
        logger.warning("AUTH: JWT validation failed — %s", exc)
        return None


async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    """
    FastAPI dependency that enforces JWT authentication.
    Returns the verified user identifier (email) on success.
    Raises HTTP 401 if the token is missing or invalid.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header. Expected: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = _decode_token(credentials.credentials)

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user_id


async def _get_current_user(
    user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
) -> User:
    """Look up the User record for the authenticated user (by email / clerk_user_id)."""
    # Try by email first (new auth), then by clerk_user_id for backwards compat
    user = (
        db.query(User).filter(User.email == user_id).first()
        or db.query(User).filter(User.clerk_user_id == user_id).first()
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found. Register via POST /api/auth/register first.",
        )
    return user


REQUIRE_ADMIN_ROLES = {Role.SUPER_ADMIN, Role.ADMIN}
REQUIRE_TECHNICIAN_ROLES = {Role.SUPER_ADMIN, Role.ADMIN, Role.TECHNICIAN}


async def require_admin(
    user: User = Depends(_get_current_user),
) -> User:
    """FastAPI dependency: require ADMIN or SUPER_ADMIN role."""
    if user.role not in REQUIRE_ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return user


async def require_technician(
    user: User = Depends(_get_current_user),
) -> User:
    """FastAPI dependency: require TECHNICIAN, ADMIN, or SUPER_ADMIN role."""
    if user.role not in REQUIRE_TECHNICIAN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Technician or admin access required.",
        )
    return user


async def require_super_admin(
    user: User = Depends(_get_current_user),
) -> User:
    """FastAPI dependency: require SUPER_ADMIN role only."""
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super admin access required.",
        )
    return user

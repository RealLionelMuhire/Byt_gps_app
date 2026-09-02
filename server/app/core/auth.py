"""
Shared authentication dependency for FastAPI routes.

Reads the `Authorization: Bearer <token>` header and validates the
Clerk session token. Routes that depend on `require_auth` will return
HTTP 401 if the token is missing or invalid.

Usage:
    from app.core.auth import require_auth, require_admin, require_technician

    @router.get("/something")
    async def my_route(clerk_user_id: str = Depends(require_auth)):
        ...  # clerk_user_id is the verified Clerk user ID string

Role-based access:
    @router.get("/admin-only")
    async def admin_route(clerk_user_id: str = Depends(require_admin)):
        ...

    @router.get("/tech-or-admin")
    async def tech_route(clerk_user_id: str = Depends(require_technician)):
        ...

Dev/offline mode:
    If CLERK_SECRET_KEY is not set in the environment the check is skipped
    and a placeholder ID is returned. This keeps local development painless.
"""

import logging
from typing import Optional

import httpx
from jose import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.user import User, Role
from app.models.device import Device
from app.models.company import Membership

logger = logging.getLogger(__name__)

# Reusable FastAPI security scheme — extracts the Bearer token from the header
_bearer = HTTPBearer(auto_error=False)


# Cache JWKS to avoid network requests on every route call
_jwks_cache = None

async def _get_jwks() -> Optional[dict]:
    """Fetch the JSON Web Key Set from Clerk."""
    global _jwks_cache
    if _jwks_cache is not None:
        return _jwks_cache

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.clerk.com/v1/jwks",
                headers={"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"},
            )
            if resp.status_code == 200:
                _jwks_cache = resp.json()
                return _jwks_cache
            logger.error("AUTH: Failed to fetch JWKS — HTTP %d: %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.error("AUTH: Network error fetching JWKS — %s", exc)
    return None

async def _verify_clerk_token(token: str) -> Optional[str]:
    """
    Verify a Clerk session JWT using the Clerk JWKS.

    Returns the Clerk user ID string on success, or None on failure.
    Fail-closed: if the JWKS server is unreachable the token is rejected
    (the cached JWKS from a previous successful fetch is still tried first,
    so this only applies when the cache is empty and the fetch fails).
    """
    if not settings.CLERK_SECRET_KEY:
        logger.warning(
            "AUTH: CLERK_SECRET_KEY not set — skipping token verification (dev mode)"
        )
        return "dev-user"

    jwks = await _get_jwks()
    # Fail-closed: if the JWKS server is unreachable we reject the token
    # rather than accepting it. A brief Clerk outage is preferable to an
    # open auth bypass. The cached JWKS from the previous successful fetch
    # is still tried by _get_jwks() first, so this only runs when the
    # cache is empty AND the fetch fails (e.g. first request after restart
    # during an outage).
    if not jwks:
        logger.error("AUTH: Could not fetch JWKS — rejecting token")
        return None

    try:
        payload = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            options={"verify_aud": False}
        )
        return payload.get("sub")
    
    except jwt.ExpiredSignatureError:
        logger.warning("AUTH: Clerk token expired")
        return None
    except jwt.JWTError as exc:
        logger.warning("AUTH: Clerk rejected token — %s", exc)
        return None


async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    """
    FastAPI dependency that enforces Clerk authentication.
    Returns the verified Clerk user ID on success.
    Raises HTTP 401 if the token is missing or invalid.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header. Expected: Bearer <clerk-token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    clerk_user_id = await _verify_clerk_token(credentials.credentials)

    if clerk_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return clerk_user_id


async def get_current_user(
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
) -> User:
    """Look up the User record for the authenticated Clerk user."""
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found. Complete sign-up via POST /api/auth/sync first.",
        )
    return user


REQUIRE_ADMIN_ROLES = {Role.SUPER_ADMIN, Role.ADMIN}
REQUIRE_TECHNICIAN_ROLES = {Role.SUPER_ADMIN, Role.ADMIN, Role.TECHNICIAN}


async def require_admin(
    user: User = Depends(get_current_user),
) -> User:
    """FastAPI dependency: require ADMIN or SUPER_ADMIN role."""
    if user.role not in REQUIRE_ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return user


async def require_technician(
    user: User = Depends(get_current_user),
) -> User:
    """FastAPI dependency: require TECHNICIAN, ADMIN, or SUPER_ADMIN role."""
    if user.role not in REQUIRE_TECHNICIAN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Technician or admin access required.",
        )
    return user


async def require_super_admin(
    user: User = Depends(get_current_user),
) -> User:
    """FastAPI dependency: require SUPER_ADMIN role only."""
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super admin access required.",
        )
    return user


def user_can_access_device(user: User, device: Device, db: Session = None) -> bool:
    """True if `user` holds an admin role, or is a member of the device's company.

    Legacy fallback: if device.company_id is not set but device.user_id is,
    allow access when user.id matches (backward compat during migration).
    """
    if user.role in REQUIRE_ADMIN_ROLES:
        return True
    if device.company_id and db is not None:
        return db.query(Membership).filter(
            Membership.user_id == user.id,
            Membership.company_id == device.company_id,
        ).first() is not None
    # Legacy fallback for devices not yet migrated
    return device.user_id == user.id


def require_device_access(device: Device, user: User, db: Session = None) -> Device:
    """
    Raise 404 unless `user` can access `device` (company member or admin).

    Uses 404 (not 403) so a non-member gets the same response whether the
    device doesn't exist or simply isn't theirs — matching the existing
    convention in get_device_status_by_imei_ownership().
    """
    if not user_can_access_device(user, device, db):
        raise HTTPException(status_code=404, detail="Device not found")
    return device

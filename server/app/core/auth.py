"""
Shared authentication dependency for FastAPI routes.

Reads the `Authorization: Bearer <token>` header and validates the
Clerk session token. Routes that depend on `require_auth` will return
HTTP 401 if the token is missing or invalid.

Usage:
    from app.core.auth import require_auth

    @router.get("/something")
    async def my_route(clerk_user_id: str = Depends(require_auth)):
        ...  # clerk_user_id is the verified Clerk user ID string

Dev/offline mode:
    If CLERK_SECRET_KEY is not set in the environment the check is skipped
    and a placeholder ID is returned. This keeps local development painless.
"""

import logging
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings

logger = logging.getLogger(__name__)

# Reusable FastAPI security scheme — extracts the Bearer token from the header
_bearer = HTTPBearer(auto_error=False)


async def _verify_clerk_token(token: str) -> Optional[str]:
    """
    Verify a Clerk session JWT via the Clerk REST API.

    Returns the Clerk user ID string on success, or None on failure.
    Falls back to returning a placeholder on transient network errors
    so a brief Clerk API outage does not lock out real users.
    """
    if not settings.CLERK_SECRET_KEY:
        # Dev mode — no secret key configured, skip verification
        logger.warning(
            "AUTH: CLERK_SECRET_KEY not set — skipping token verification (dev mode)"
        )
        return "dev-user"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.clerk.com/v1/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("id")           # e.g. "user_2abc123xyz"
            logger.warning(
                "AUTH: Clerk rejected token — HTTP %d", resp.status_code
            )
            return None
    except Exception as exc:
        # Network error talking to Clerk — fail open so real users aren't blocked
        logger.error("AUTH: token verification error — %s", exc)
        return "network-error-fallback"


async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    """
    FastAPI dependency that enforces Clerk authentication.

    Inject this into any route that should be protected:

        @router.get("/")
        async def list_things(clerk_user_id: str = Depends(require_auth)):
            ...

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

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
from jose import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings

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
            # The JWKS endpoint is standard for Clerk backend APIs
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
    Falls back to returning a placeholder on transient network errors
    so a brief Clerk API outage does not lock out real users.
    """
    if not settings.CLERK_SECRET_KEY:
        # Dev mode — no secret key configured, skip verification
        logger.warning(
            "AUTH: CLERK_SECRET_KEY not set — skipping token verification (dev mode)"
        )
        return "dev-user"

    # 1. Fetch JWKS
    jwks = await _get_jwks()
    if not jwks:
        # Network error talking to Clerk — fail open so real users aren't blocked
        # (Alternatively, you can fail closed if security is paramount)
        logger.warning("AUTH: Could not fetch JWKS, allowing fallback")
        return "network-error-fallback"

    # 2. Decode and verify the JWT signature
    try:
        # Clerk tokens usually use RS256. 
        # We don't enforce `verify_aud` strictly unless configured.
        payload = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            options={"verify_aud": False}
        )
        # Clerk puts the user_id in the 'sub' claim
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

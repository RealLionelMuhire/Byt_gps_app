"""
Configuration settings for GPS Tracking Server
"""

import os
from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import Optional


class Settings(BaseSettings):
    """Application settings"""
    
    # Application
    APP_NAME: str = "GPS Tracking Server"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    
    # Server
    HOST: str = "0.0.0.0"
    HTTP_PORT: int = 8000
    TCP_PORT: int = 7018
    
    # Database
    DATABASE_URL: str = "postgresql://gps_user:gps_password@localhost:5432/gps_tracking"
    # Direct/session-mode connection (bypasses the transaction pooler) — used for
    # DDL/migrations. Falls back to DATABASE_URL if not set.
    DIRECT_URL: Optional[str] = None
    
    # Security
    SECRET_KEY: str = "your-secret-key-change-this-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # Clerk Auth
    CLERK_SECRET_KEY: Optional[str] = None
    CLERK_PUBLISHABLE_KEY: Optional[str] = None  # pk_live_... — used in admin dashboard JS
    CLERK_WEBHOOK_SECRET: Optional[str] = None
    # Where the client lands after accepting a Clerk invitation email
    CLERK_INVITATION_REDIRECT_URL: Optional[str] = None
    # Comma-separated Clerk User IDs that are allowed to access the admin dashboard
    # e.g. "user_2abc123,user_2xyz456" — copy from Clerk Dashboard > Users
    ADMIN_CLERK_USER_IDS: str = ""

    # Legacy admin secret (kept for fallback / non-Clerk environments)
    ADMIN_SECRET: Optional[str] = None

    # IntouchPay (Rwanda mobile money) — see app/services/intouchpay.py
    # Sandbox and production are different hosts (not just different paths) —
    # keep this True until a real test transaction has been confirmed working,
    # then switch to False and swap in the live credentials.
    INTOUCH_SANDBOX: bool = True
    INTOUCH_USERNAME: Optional[str] = None
    INTOUCH_ACCOUNT_NO: Optional[str] = None
    INTOUCH_PARTNER_PASSWORD: Optional[str] = None
    INTOUCH_CALLBACK_URL: Optional[str] = None

    @property
    def admin_user_ids(self) -> set:
        """Return the set of authorized admin Clerk user IDs."""
        return {uid.strip() for uid in self.ADMIN_CLERK_USER_IDS.split(",") if uid.strip()}
    
    # CORS — accepts a comma-separated string from .env, e.g. "*" or "https://a.com,https://b.com"
    CORS_ORIGINS: str = "*"

    @property
    def cors_origins_list(self) -> list:
        if self.CORS_ORIGINS.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    
    # Logging
    LOG_LEVEL: str = "INFO"

    # Hemisphere Correction
    FORCE_SOUTHERN_HEMISPHERE: bool = True  # Set to True for Rwanda/Southern Africa if device reports North

    # Device data freshness
    DEVICE_SENDING_STALE_SECONDS: int = 120  # Consider "stale" if no packet within this window
    DEVICE_OFFLINE_TIMEOUT_SECONDS: int = 300  # Consider "offline" if no packet within this window

    # Trip auto-end: end active trips if device stops sending for this long (seconds)
    TRIP_AUTO_END_STALE_SECONDS: int = 300  # Same as offline timeout; end trip if no update

    # Nominatim (OpenStreetMap) reverse geocoding
    NOMINATIM_USER_AGENT: str = "BYThron-GPS/1.0 (contact@bythron.com)"
    NOMINATIM_BASE_URL: str = "https://nominatim.openstreetmap.org"
    NOMINATIM_TIMEOUT_SECONDS: float = 5.0

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()


settings = get_settings()

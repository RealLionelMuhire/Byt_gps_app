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
    
    # Security
    SECRET_KEY: str = "your-secret-key-change-this-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # Clerk Auth
    CLERK_SECRET_KEY: Optional[str] = None
    CLERK_PUBLISHABLE_KEY: Optional[str] = None  # pk_live_... — used in admin dashboard JS
    # Comma-separated Clerk User IDs that are allowed to access the admin dashboard
    # e.g. "user_2abc123,user_2xyz456" — copy from Clerk Dashboard > Users
    ADMIN_CLERK_USER_IDS: str = ""

    # Legacy admin secret (kept for fallback / non-Clerk environments)
    ADMIN_SECRET: Optional[str] = None
    FLUTTERWAVE_SECRET_KEY: Optional[str] = None

    @property
    def admin_user_ids(self) -> set:
        """Return the set of authorized admin Clerk user IDs."""
        return {uid.strip() for uid in self.ADMIN_CLERK_USER_IDS.split(",") if uid.strip()}
    
    # CORS
    CORS_ORIGINS: list = ["*"]
    
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

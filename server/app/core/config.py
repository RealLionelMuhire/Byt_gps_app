"""
Configuration settings for GPS Tracking Server
"""

import os
from pydantic_settings import BaseSettings
from functools import lru_cache


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

"""Persistent cache of Nominatim reverse-geocoding results."""

from datetime import datetime

from sqlalchemy import Column, Integer, Float, String, DateTime, UniqueConstraint

from app.core.database import Base


class GeocodeCache(Base):
    """
    Reverse-geocoding result cache, keyed by lat/lon rounded to 3 decimal
    places (~100m precision -- see geocoding._CACHE_PRECISION). Only
    successful resolutions are stored; failed/no-result lookups are not
    persisted here, so they're retried on the next request rather than
    cached as a permanent miss.
    """
    __tablename__ = "geocode_cache"
    __table_args__ = (UniqueConstraint("lat", "lon", name="uq_geocode_cache_lat_lon"),)

    id = Column(Integer, primary_key=True, index=True)
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    place_name = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<GeocodeCache(lat={self.lat}, lon={self.lon}, place_name={self.place_name!r})>"

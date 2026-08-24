"""GPS quality diagnostics log — one row per ingested location point.

Audit trail / tuning input for the quality heuristics in app/api/locations.py
(MAX_PLAUSIBLE_SPEED_KMH, TIME_GAP_SEGMENT_BREAK_SECONDS): captures the raw
signals and derived flags at the time each point was evaluated, independent
of the `locations` table so it isn't affected by any later changes to the
points themselves.
"""

from sqlalchemy import Column, Integer, Float, Boolean, DateTime, ForeignKey, Index
from sqlalchemy.orm import relationship
from datetime import datetime

from app.core.database import Base


class LocationQualityLog(Base):
    """Per-point GPS quality diagnostics."""
    __tablename__ = "location_quality_log"

    id = Column(Integer, primary_key=True, index=True)
    location_id = Column(Integer, ForeignKey("locations.id"), nullable=False, unique=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False)  # denormalized from locations.timestamp for range queries

    satellites = Column(Integer, nullable=False)
    # Implied speed vs. the previous point (haversine/Δt); None for a device's first point.
    implied_speed_kmh = Column(Float, nullable=True)
    # Angular difference (0-180°) between reported course and the prev->curr
    # movement bearing; None when movement is too small for a movement
    # bearing to be meaningful, or there's no previous point.
    course_delta_degrees = Column(Float, nullable=True)
    # Seconds since the previous point for this device; None for the first point.
    gap_seconds = Column(Float, nullable=True)
    is_outlier = Column(Boolean, nullable=False, default=False)
    is_segment_break = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    location = relationship("Location")

    __table_args__ = (
        Index("ix_location_quality_log_device_timestamp", "device_id", "timestamp"),
    )

    def __repr__(self):
        return f"<LocationQualityLog(location_id={self.location_id}, device_id={self.device_id})>"

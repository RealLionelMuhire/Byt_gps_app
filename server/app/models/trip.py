"""Trip model - saved trip metadata"""

from sqlalchemy import Column, Integer, String, DateTime, Float, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from app.core.database import Base


class Trip(Base):
    """Saved trip - user-defined time range over device locations"""
    __tablename__ = "trips"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    name = Column(String(255), nullable=False)
    display_name = Column(String(512), nullable=True)  # Human-readable from reverse geocoding
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=True)  # Null = active trip (auto-ended when device stops)
    total_distance_km = Column(Float, nullable=False, default=0.0)

    # Optional: first/last location references
    start_location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)
    end_location_id = Column(Integer, ForeignKey("locations.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    device = relationship("Device", backref="trips")
    user = relationship("User", backref="trips")
    start_location = relationship("Location", foreign_keys=[start_location_id])
    end_location = relationship("Location", foreign_keys=[end_location_id])

    def __repr__(self):
        return f"<Trip(id={self.id}, device_id={self.device_id}, name='{self.name}')>"

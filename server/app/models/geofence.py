"""Geofence model"""

from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from geoalchemy2 import Geometry
from datetime import datetime

from app.core.database import Base


class Geofence(Base):
    """
    Geofence (virtual boundary), evaluated server-side on every incoming
    location fix (see app/services/geofencing.py). Neither supported
    device model (TK903ELE, G900LS J16-4G) exposes a command to push a
    zone definition to the hardware, so the device's own GT06 fence
    enter/exit alarm bytes (0x04/0x05) can never be configured from this
    backend and aren't relied on — the server computes breaches itself.

    v1 supports circle zones only (center_latitude/longitude/radius_meters).
    `geom` is reserved for a future polygon geofence type; evaluate_geofences
    ignores rows where the circle fields are NULL.
    """
    __tablename__ = "geofences"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)

    # Geometry (polygon) — not yet evaluated; reserved for a future polygon geofence type.
    geom = Column(Geometry('POLYGON', srid=4326), nullable=True)

    # Circle center + radius — the only zone type evaluated in v1.
    center_latitude = Column(Float, nullable=True)
    center_longitude = Column(Float, nullable=True)
    radius_meters = Column(Float, nullable=True)

    # Settings
    is_active = Column(Boolean, nullable=False, default=True)
    alert_on_enter = Column(Boolean, nullable=False, default=True)
    alert_on_exit = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", backref="geofences")
    device_states = relationship(
        "GeofenceDeviceState", back_populates="geofence",
        cascade="all, delete-orphan", passive_deletes=True,
    )

    def __repr__(self):
        return f"<Geofence(name='{self.name}', active={self.is_active})>"

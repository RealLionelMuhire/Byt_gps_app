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

    Two shapes, selected by `shape_type`:
      - "circle": center_latitude/center_longitude/radius_meters (haversine
        check against radius — see evaluate_geofences).
      - "polygon": `geom`, evaluated via PostGIS ST_Contains.
    Exactly one shape's fields are populated per row (enforced by the
    geofences_shape_matches_type CHECK constraint added in migration 025).
    """
    __tablename__ = "geofences"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)

    shape_type = Column(String(10), nullable=False, default="circle", server_default="circle")

    # Geometry (polygon) — populated only when shape_type == "polygon".
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

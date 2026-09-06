"""Geofence-device scoping — which devices a geofence actually applies to."""

from sqlalchemy import Column, Integer, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime

from app.core.database import Base


class GeofenceDevice(Base):
    """
    One row = this geofence applies to this device.

    A geofence with zero rows here applies to NO devices — not "all of the
    owner's devices" — until explicitly assigned via the CRUD API's
    device_ids. That's a deliberate choice (see app/api/geofences.py):
    silently defaulting an empty assignment to "everything" is easy to
    misread as "nothing assigned yet, harmless", when it actually means
    "fires for the whole fleet".

    app.services.geofencing.evaluate_geofences joins on this table (in
    addition to the existing Geofence.user_id/is_active/shape_type
    filters) so both circle and polygon zones share the same scoping
    without either evaluation branch needing its own device check.
    """
    __tablename__ = "geofence_devices"
    __table_args__ = (
        UniqueConstraint("geofence_id", "device_id", name="uq_geofence_devices_pair"),
    )

    id = Column(Integer, primary_key=True, index=True)
    geofence_id = Column(Integer, ForeignKey("geofences.id", ondelete="CASCADE"), nullable=False, index=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    geofence = relationship("Geofence", back_populates="device_links")
    device = relationship("Device")

    def __repr__(self):
        return f"<GeofenceDevice(geofence_id={self.geofence_id}, device_id={self.device_id})>"

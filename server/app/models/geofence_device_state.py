"""Geofence device state model - tracks per-device inside/outside state per geofence"""

from sqlalchemy import Column, Integer, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime

from app.core.database import Base


class GeofenceDeviceState(Base):
    """
    Persisted enter/exit state for one (device, geofence) pair.

    app.services.geofencing.evaluate_geofences reads/writes this to detect
    actual transitions across restarts, rather than re-deriving state from
    location history on every ping. One row per pair: the row is created
    (without firing an event) the first time a device is ever evaluated
    against a geofence — otherwise a device already inside a brand-new
    zone would fire a spurious "Enter fence" the moment it's first
    evaluated — then updated in place on every subsequent ping.
    """
    __tablename__ = "geofence_device_state"
    __table_args__ = (
        UniqueConstraint("device_id", "geofence_id", name="uq_geofence_device_state_pair"),
    )

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True)
    geofence_id = Column(Integer, ForeignKey("geofences.id", ondelete="CASCADE"), nullable=False, index=True)
    is_inside = Column(Boolean, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    device = relationship("Device")
    geofence = relationship("Geofence", back_populates="device_states")

    def __repr__(self):
        return f"<GeofenceDeviceState(device_id={self.device_id}, geofence_id={self.geofence_id}, is_inside={self.is_inside})>"

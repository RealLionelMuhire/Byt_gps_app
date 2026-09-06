"""Location model"""

from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from geoalchemy2 import Geometry
from datetime import datetime

from app.core.database import Base


class Location(Base):
    """GPS Location Record"""
    __tablename__ = "locations"
    
    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey('devices.id'), nullable=False, index=True)
    
    # GPS coordinates
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    # PostGIS geometry point for spatial queries
    geom = Column(Geometry('POINT', srid=4326), nullable=True)
    
    # GPS data
    speed = Column(Float, default=0)  # km/h
    course = Column(Integer, default=0)  # 0-360 degrees
    satellites = Column(Integer, default=0)
    gps_valid = Column(Boolean, default=False)
    # Implausible GPS jump (see MAX_PLAUSIBLE_SPEED_KMH in app/api/locations.py). Flagged, never deleted.
    is_outlier = Column(Boolean, nullable=False, default=False)
    
    # Alarm info
    is_alarm = Column(Boolean, default=False)
    alarm_type = Column(String(50), nullable=True)

    # Alarm notification-delivery discipline (see app/services/alarm_rules.py
    # and TCPServer._send_push_notification in app/tcp_server.py):
    # - acknowledged_at: set when the user views/acknowledges this alarm in the app.
    # - escalated_at: set once the one-time unacknowledged-critical-alarm resend has fired.
    # - digested_at: set once this alarm has been "accounted for" via push (sent
    #   immediately, explicitly muted, or folded into a digest) — NULL means it's
    #   still awaiting the periodic low/medium digest job.
    acknowledged_at = Column(DateTime, nullable=True)
    escalated_at = Column(DateTime, nullable=True)
    digested_at = Column(DateTime, nullable=True)

    # Timestamps
    timestamp = Column(DateTime, nullable=False, index=True)  # GPS tracker time
    received_at = Column(DateTime, default=datetime.utcnow)  # Server receive time
    
    # Relationship
    device = relationship("Device", back_populates="locations")
    
    def __repr__(self):
        return f"<Location(device_id={self.device_id}, lat={self.latitude}, lon={self.longitude}, time='{self.timestamp}')>"

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
    
    # Alarm info
    is_alarm = Column(Boolean, default=False)
    alarm_type = Column(String(50), nullable=True)
    
    # Timestamps
    timestamp = Column(DateTime, nullable=False, index=True)  # GPS tracker time
    received_at = Column(DateTime, default=datetime.utcnow)  # Server receive time
    
    # Relationship
    device = relationship("Device", back_populates="locations")
    
    def __repr__(self):
        return f"<Location(device_id={self.device_id}, lat={self.latitude}, lon={self.longitude}, time='{self.timestamp}')>"

"""Geofence model"""

from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean
from geoalchemy2 import Geometry
from datetime import datetime

from app.core.database import Base


class Geofence(Base):
    """Geofence (virtual boundary)"""
    __tablename__ = "geofences"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    
    # Geometry (polygon or circle)
    geom = Column(Geometry('POLYGON', srid=4326), nullable=False)
    
    # Circle center (if circle type)
    center_latitude = Column(Float, nullable=True)
    center_longitude = Column(Float, nullable=True)
    radius_meters = Column(Float, nullable=True)
    
    # Settings
    is_active = Column(Boolean, default=True)
    alert_on_enter = Column(Boolean, default=True)
    alert_on_exit = Column(Boolean, default=True)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f"<Geofence(name='{self.name}', active={self.is_active})>"

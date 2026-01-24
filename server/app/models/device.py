"""Device model"""

from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime

from app.core.database import Base


class Device(Base):
    """GPS Tracker Device"""
    __tablename__ = "devices"
    
    id = Column(Integer, primary_key=True, index=True)
    imei = Column(String(20), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    
    # Status
    status = Column(String(20), default='offline')  # online, offline, inactive
    last_connect = Column(DateTime, nullable=True)
    last_update = Column(DateTime, nullable=True)
    
    # Last known location
    last_latitude = Column(Float, nullable=True)
    last_longitude = Column(Float, nullable=True)
    
    # Device info
    battery_level = Column(Integer, nullable=True)  # 0-100
    gsm_signal = Column(Integer, nullable=True)  # 0-31
    
    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    locations = relationship("Location", back_populates="device", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<Device(imei='{self.imei}', name='{self.name}', status='{self.status}')>"

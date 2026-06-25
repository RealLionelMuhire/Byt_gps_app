"""Device model"""

from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from app.core.database import Base


class Device(Base):
    """
    GPS Tracker Device

    Lifecycle states (lifecycle column):
    ┌─────────────┬───────────────────────────────────────────────────────────┐
    │  State      │ Meaning                                                   │
    ├─────────────┼───────────────────────────────────────────────────────────┤
    │ registered  │ Admin added IMEI to DB and inserted SIM card.             │
    │             │ Device has never connected via TCP.                       │
    │             │ NOT yet ready to sell.                                    │
    ├─────────────┼───────────────────────────────────────────────────────────┤
    │ in_stock    │ Device has sent at least one TCP handshake (0x01 login).  │
    │             │ Proven to be functional and online. Ready to sell.        │
    │             │ Still owned by us (user_id = NULL).                       │
    ├─────────────┼───────────────────────────────────────────────────────────┤
    │ sold        │ Device is paired to a customer account (user_id IS SET).  │
    │             │ Customer has full ownership and control.                   │
    └─────────────┴───────────────────────────────────────────────────────────┘

    Transitions:
      registered  → in_stock : TCP handshake received (automatic, via tcp_server.py)
      in_stock    → sold     : Customer pairs device via POST /api/devices/pair
      sold        → in_stock : If customer is removed / device returned (admin action)
    """
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    imei = Column(String(20), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    pairing_pin = Column(String(8), nullable=True)  # Secret PIN printed inside device box
    sim_number = Column(String(20), nullable=True)   # Phone number of the SIM card inside device
    hardware_model = Column(String(50), nullable=True) # e.g. 'G900LS J16-4G', 'TK903ELE'
    sim_renewal_date = Column(DateTime, nullable=True) # When the SIM airtime/data expires

    # Inventory lifecycle
    # Values: 'registered' | 'in_stock' | 'sold'
    lifecycle = Column(String(20), nullable=False, default='registered')

    # User ownership (NULL = owned by company, set = owned by customer)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)

    # TCP connection status (independent of lifecycle)
    status = Column(String(20), default='offline')   # online | offline
    last_connect = Column(DateTime, nullable=True)    # Last TCP handshake received
    last_update = Column(DateTime, nullable=True)     # Last location/heartbeat received

    # Last known location
    last_latitude = Column(Float, nullable=True)
    last_longitude = Column(Float, nullable=True)

    # Device telemetry
    battery_level = Column(Integer, nullable=True)  # 0-100
    gsm_signal = Column(Integer, nullable=True)     # 0-31

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", backref="devices")
    locations = relationship("Location", back_populates="device", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Device(imei='{self.imei}', lifecycle='{self.lifecycle}', status='{self.status}')>"

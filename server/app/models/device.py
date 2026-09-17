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

    # Map marker glyph. Values: 'arrow' (default) | 'sedan' | 'truck_small' |
    # 'truck_big' | 'bus' | 'animal' — validated at the API layer (see
    # DeviceMarkerIconUpdate in app/api/devices.py), not a DB constraint,
    # matching this model's existing convention for lifecycle/status.
    marker_icon = Column(String(20), nullable=False, default='arrow')

    # Inventory lifecycle
    # Values: 'registered' | 'in_stock' | 'sold'
    lifecycle = Column(String(20), nullable=False, default='registered')

    # User ownership (NULL = owned by company, set = owned by customer)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, index=True)

    # DEPRECATED — device-level plan assignment was retired (Phase 1 of the
    # plan/subscription consolidation). A device's effective plan is always
    # resolved from its owner's own Subscription — see
    # app/services/plan_resolution.py — never from this column. Nothing
    # writes to it anymore (PUT /api/devices/{id}/plan and
    # POST /admin/devices/{imei}/plan both 410/redirect-refuse now); it's
    # kept only so historical rows and the FK stay intact rather than force
    # a data migration in the same pass. Do not read or write it elsewhere.
    plan_id = Column(Integer, ForeignKey('subscription_plans.id'), nullable=True, index=True)

    # TCP connection status (independent of lifecycle)
    status = Column(String(20), default='offline')   # online | offline
    last_connect = Column(DateTime, nullable=True)    # Last TCP handshake received
    last_update = Column(DateTime, nullable=True)     # Last location/heartbeat received

    # Last known location — the CONFIRMED live position (see
    # app/services/live_position.py). Never written from a raw ping
    # directly; only resolve_live_position() updates these.
    last_latitude = Column(Float, nullable=True)
    last_longitude = Column(Float, nullable=True)
    # When last_latitude/last_longitude were last actually changed by a
    # confirmed fix (see migration 037) — distinct from last_update below,
    # which is bumped on every packet whether or not it was valid or moved
    # the position. Lets clients show "position confirmed 3h ago" instead
    # of implying a live position from a recent-but-invalid ping.
    position_confirmed_at = Column(DateTime, nullable=True)

    # Unconfirmed live-position candidate awaiting a corroborating next
    # point, staged when a ping jumps away from last_latitude/longitude
    # while the device reports itself as stopped (see migration 031 and
    # app/services/live_position.py). NULL when nothing is being held.
    pending_latitude = Column(Float, nullable=True)
    pending_longitude = Column(Float, nullable=True)
    pending_since = Column(DateTime, nullable=True)

    # Device telemetry
    battery_level = Column(Integer, nullable=True)  # 0-100
    gsm_signal = Column(Integer, nullable=True)     # 0-31

    # Owner-configured speed threshold (km/h) — see
    # app/services/speed_limit.py, which evaluates this on every incoming
    # location fix (migration 029). NULL = no custom limit; the device's
    # own fixed-firmware "Over speed" alarm (if any) is unaffected either
    # way, since neither supported hardware model exposes a way to read or
    # set that value from this backend.
    speed_limit_kmh = Column(Float, nullable=True)
    # Edge-detection state for speed_limit_kmh — True while the most recent
    # fix was over the limit. Purely internal bookkeeping for
    # app/services/speed_limit.py; never exposed via the API.
    is_overspeeding = Column(Boolean, nullable=False, default=False)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", backref="devices")
    plan = relationship("SubscriptionPlan", foreign_keys=[plan_id])  # deprecated, see plan_id above
    locations = relationship("Location", back_populates="device", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Device(imei='{self.imei}', lifecycle='{self.lifecycle}', status='{self.status}')>"

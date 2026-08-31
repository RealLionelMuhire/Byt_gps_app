"""Command settings model - per-device policy for remote commands"""

from sqlalchemy import Column, Integer, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from app.core.database import Base


class CommandSettings(Base):
    """
    Per-device policy for the remote-command endpoints in app/api/commands.py.

    One row per device. Absence of a row means "use the hardcoded defaults"
    below (require_confirmation_for_fuel_cut=True, fuel_cut_enabled=True,
    admin_only=False, commands_per_hour=30) — same convention as
    TripSettings.
    """
    __tablename__ = "command_settings"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    # Require an explicit confirmation step (enforced at the API layer, e.g.
    # a "confirm": true body field on POST /{device_id}/fuel/cut) before the
    # one destructive command in commands.py — immobilizing the vehicle — is
    # sent to the device.
    require_confirmation_for_fuel_cut = Column(Boolean, nullable=False, default=True)

    # Hard block on fuel/cut and fuel/restore for this device, independent of
    # role or ownership. Some fleets/contracts don't want remote
    # immobilization available at all (liability).
    fuel_cut_enabled = Column(Boolean, nullable=False, default=True)

    # If True, only SUPER_ADMIN/ADMIN (user.is_admin) may send ANY command to
    # this device — the owning USER is blocked. Layered on top of, not a
    # replacement for, require_device_access()'s ownership check. There's no
    # ranked-role hierarchy in this codebase (see Role in models/user.py), so
    # this is a binary gate rather than a "minimum role" tier.
    admin_only = Column(Boolean, nullable=False, default=False)

    # Simple fixed-window rate limit: max commands accepted for this device,
    # across every endpoint in app/api/commands.py, per rolling hour.
    commands_per_hour = Column(Integer, nullable=False, default=30)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship
    device = relationship("Device", backref="command_settings", uselist=False)

    def __repr__(self):
        return f"<CommandSettings(device_id={self.device_id}, fuel_cut_enabled={self.fuel_cut_enabled}, admin_only={self.admin_only})>"

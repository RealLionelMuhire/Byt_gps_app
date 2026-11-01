"""Tracker hardware models and their command tables (migration 057)."""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.database import Base


class TrackerModel(Base):
    """One GPS tracker hardware model (e.g. "G900LS J16-4G", "TK903ELE").

    Every model must speak a wire protocol this server parses — today only
    GT06 (0x7878 packets); a new protocol needs a parser in code, not just
    a row here. What differs between GT06 models is the command vocabulary,
    which lives in TrackerModelCommand rows: the same capability ("cut
    fuel") is `RELAY,1#` on one model and `stop123456` on another.

    Devices without a model use the `is_default` one (exactly one should be
    — the migration seeds the G900LS, the production hardware).
    """

    __tablename__ = "tracker_models"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(80), nullable=False, unique=True)
    manufacturer = Column(String(80), nullable=True)
    protocol = Column(String(20), nullable=False, default="gt06")
    # Substituted for {password} in command templates (SMS-style commands
    # like "stop123456" embed the tracker's password).
    command_password = Column(String(40), nullable=True)
    notes = Column(Text, nullable=True)
    is_default = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    # Optional regexes for reading this model's replies to fuel commands —
    # JSON {"relay_cut": ..., "relay_restored": ..., "relay_deferred": ...}.
    # Checked before the built-in GT06/G900LS rules (app/services/relay_state.py).
    reply_patterns = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    commands = relationship(
        "TrackerModelCommand", back_populates="tracker_model",
        cascade="all, delete-orphan", order_by="TrackerModelCommand.key",
    )


class TrackerModelCommand(Base):
    """The exact command text a model uses for one capability (a key from
    app/services/tracker_capabilities.py) or for an admin-only extra
    ("extra.<slug>").

    `template` may use {password} and the capability's parameter names,
    e.g. "SENALM,ON,{mode}#". `verified` is set by an admin once the
    command has been tried on a real tracker from the portal; customers
    are only ever offered verified commands.
    """

    __tablename__ = "tracker_model_commands"
    __table_args__ = (UniqueConstraint("tracker_model_id", "key", name="uq_tracker_model_command_key"),)

    id = Column(Integer, primary_key=True, index=True)
    tracker_model_id = Column(Integer, ForeignKey("tracker_models.id", ondelete="CASCADE"), nullable=False, index=True)
    key = Column(String(64), nullable=False)
    label = Column(String(80), nullable=True)  # required for extras; catalog label otherwise
    template = Column(String(200), nullable=False)
    verified = Column(Boolean, nullable=False, default=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    tracker_model = relationship("TrackerModel", back_populates="commands")

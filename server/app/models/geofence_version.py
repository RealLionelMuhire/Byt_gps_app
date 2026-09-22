"""Geofence version history — what a zone looked like, and when."""

from sqlalchemy import Column, Integer, String, DateTime, Float, Boolean, ForeignKey, JSON, Index

from app.core.database import Base


class GeofenceVersion(Base):
    """
    One row = one period during which a geofence had a given state
    (name, shape, active flag, assigned devices), from `valid_from` up to
    (not including) `valid_to`. `valid_to` NULL means "still current".

    `geofences` itself only ever holds the CURRENT state — toggling a zone
    off, moving it, or reassigning its devices overwrites the old values,
    and deleting it removes the row. Historical Routes needs to draw only
    the zones that were actually in effect for a device during a past
    period, so app/api/geofences.py appends a row here on every create/
    update/delete (see _record_version) and GET /api/geofences/history
    reads from it.

    Deliberately NOT a foreign key on geofence_id: a deleted geofence's
    history must survive its deletion (its last version is closed off at
    delete time, not cascaded away). Snapshot columns rather than a
    Geometry: polygon points are stored as a JSON [{lat, lng}] list — the
    exact shape the API returns — since nothing ever needs to run a
    spatial query against history, only redraw it.
    """
    __tablename__ = "geofence_versions"
    __table_args__ = (
        Index("idx_geofence_versions_user_window", "user_id", "valid_from", "valid_to"),
        Index("idx_geofence_versions_geofence_open", "geofence_id", "valid_to"),
    )

    id = Column(Integer, primary_key=True, index=True)
    geofence_id = Column(Integer, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    name = Column(String(100), nullable=False)
    shape_type = Column(String(10), nullable=False)
    center_latitude = Column(Float, nullable=True)
    center_longitude = Column(Float, nullable=True)
    radius_meters = Column(Float, nullable=True)
    points = Column(JSON, nullable=True)
    device_ids = Column(JSON, nullable=False, default=list)
    is_active = Column(Boolean, nullable=False)

    # Naive UTC, matching every other timestamp column in this schema.
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<GeofenceVersion(geofence_id={self.geofence_id}, {self.valid_from}..{self.valid_to})>"

"""
Database connection and session management
"""

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from geoalchemy2 import Geometry

from app.core.config import settings

# Neon serverless works best with a small pool and explicit SSL.
# pool_size=5 avoids exhausting Neon's free-tier connection limits.
# pool_recycle prevents stale connections after Neon auto-suspends.
_connect_args = {}
if "neon.tech" in settings.DATABASE_URL or "sslmode=require" in settings.DATABASE_URL:
    _connect_args = {"sslmode": "require"}

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,     # Verify connection liveness before use
    pool_size=5,            # Neon free tier: keep pool small
    max_overflow=10,
    pool_recycle=300,       # Recycle after 5 min (Neon suspends idle computes)
    connect_args=_connect_args,
)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()


def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Initialize database - create all tables"""
    from sqlalchemy import text

    # Enable PostGIS extension first — required for geometry columns.
    # On Neon (plain PostgreSQL), PostGIS is available but must be explicitly enabled.
    # On the local postgis/postgis Docker image it is already active, so this is a no-op.
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
        conn.commit()

    # Import all models here
    from app.models import device, location, user, geofence, trip, trip_settings

    # Create tables
    Base.metadata.create_all(bind=engine)

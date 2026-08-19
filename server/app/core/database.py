"""
Database connection and session management
"""

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from geoalchemy2 import Geometry

from app.core.config import settings

# Supabase's pgbouncer pooler (transaction mode, port 6543) already multiplexes
# connections server-side, so SQLAlchemy's own pool is kept small on top of it.
# psycopg2 doesn't use server-side prepared statements, so it's safe to run
# through transaction-mode pgbouncer without extra config.
_connect_args = {}
if "supabase.com" in settings.DATABASE_URL or "neon.tech" in settings.DATABASE_URL or "sslmode=require" in settings.DATABASE_URL:
    _connect_args = {"sslmode": "require"}

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,     # Verify connection liveness before use
    pool_size=5,            # Keep small: pgbouncer already pools upstream
    max_overflow=10,
    pool_recycle=300,       # Recycle after 5 min to avoid stale pooled connections
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
    # Supabase ships PostGIS but it must be explicitly enabled per-database.
    # On the local postgis/postgis Docker image it is already active, so this is a no-op.
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
        # Create the user_role enum type if it doesn't exist (for fresh installs)
        conn.execute(text("""
            DO $$ BEGIN
                CREATE TYPE user_role AS ENUM ('SUPER_ADMIN', 'ADMIN', 'TECHNICIAN', 'USER');
            EXCEPTION
                WHEN duplicate_object THEN NULL;
            END $$;
        """))
        conn.commit()

    # Import all models here
    from app.models import device, location, user, geofence, trip, trip_settings

    # Create tables
    Base.metadata.create_all(bind=engine)

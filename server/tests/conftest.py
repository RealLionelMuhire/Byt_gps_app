"""
Shared pytest fixtures for API route tests.

Route tests spin up a real FastAPI app (the actual `devices` and
`onboarding` routers — not reimplementations) against an isolated
in-memory SQLite database, and override auth to a controllable
clerk_user_id. `get_db`/`require_auth` are overridden; everything else
(ownership checks, business logic) runs unmodified.

Two columns (locations.geom, geofences.geom) use geoalchemy2's Geometry
type, which compiles to PostGIS/SpatiaLite-only DDL and SQL functions
(RecoverGeometryColumn, AsEWKB, ...) that plain SQLite doesn't have. Rather
than hand-picking which tables to create — Device has backref
relationships from nearly every other table (trips, alert_settings,
command_settings, ...), and SQLAlchemy's flush needs all of them to exist
to compute delete/cascade history — every table is created, with those
few SpatiaLite function names stubbed out as harmless no-ops on the test
connection. No test in this suite reads/writes geometry data, so the
stubs are never exercised for real.
"""

import importlib
import os
import pkgutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ.setdefault("CLERK_SECRET_KEY", "")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.auth import require_auth
from app.api import devices, onboarding

# Import every model module so all Base.metadata tables are registered —
# relied on below by Base.metadata.create_all() with no `tables=` filter.
# Needed because Device has backref relationships from models the
# devices/onboarding routers never import directly (e.g. CommandSettings),
# and SQLAlchemy needs every such table to exist to compute delete/cascade
# history during a flush, not just the ones this test file touches.
import app.models as _models_pkg
for _, _modname, _ in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_modname}")

# SpatiaLite functions geoalchemy2 emits DDL/SQL for on a "sqlite" dialect;
# stubbed as no-ops purely so CREATE TABLE / cascade queries succeed.
_SPATIALITE_STUBS = [
    ("AsEWKB", 1), ("RecoverGeometryColumn", 5), ("DiscardGeometryColumn", 2),
    ("AddGeometryColumn", -1), ("CreateSpatialIndex", 2), ("DisableSpatialIndex", 2),
]


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _register_spatialite_stubs(dbapi_conn, conn_record):
        for name, argc in _SPATIALITE_STUBS:
            dbapi_conn.create_function(name, argc, lambda *a: 1)

    Base.metadata.create_all(bind=engine)

    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def current_clerk_id():
    """Mutable box the test controls; the require_auth override reads it on
    every request, so a test can switch identity mid-test (e.g. owner then
    admin) just by writing to this dict."""
    return {"value": "clerk_default_test_user"}


@pytest.fixture()
def client(db_session, current_clerk_id):
    app = FastAPI()
    app.include_router(devices.router, prefix="/api/devices")
    app.include_router(onboarding.router, prefix="/api")

    def _override_get_db():
        yield db_session

    async def _override_require_auth():
        return current_clerk_id["value"]

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_auth] = _override_require_auth

    with TestClient(app) as c:
        yield c

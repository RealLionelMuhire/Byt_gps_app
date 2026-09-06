"""
Shared pytest fixtures for API route tests.

Route tests spin up a real FastAPI app (the actual `devices` and
`onboarding` routers — not reimplementations) against an isolated
in-memory SQLite database, and override auth to a controllable
clerk_user_id. `get_db`/`require_auth` are overridden; everything else
(ownership checks, business logic) runs unmodified.

Two columns (locations.geom, geofences.geom) use geoalchemy2's Geometry
type, which compiles to PostGIS/SpatiaLite-only DDL and SQL functions
(RecoverGeometryColumn, AsEWKB, GeomFromEWKT, ...) that plain SQLite
doesn't have — GeomFromEWKT in particular is emitted by geoalchemy2 to
wrap *every* insert/update bind for a Geometry column, even when the
Python value is None, so any ORM insert into a geometry-bearing table
needs it stubbed. Rather than hand-picking which tables to create — Device
has backref relationships from nearly every other table (trips,
alert_settings, command_settings, ...), and SQLAlchemy's flush needs all
of them to exist to compute delete/cascade history — every table is
created, with those SpatiaLite function names stubbed out as harmless
no-ops (returning a dummy value) on the test connection.

locations.geom stays NULL in every test, so its dummy value is never
exercised for real. geofences.geom is different: polygon geofence tests
(test_geofencing.py, test_geofences_api.py) genuinely need correct
point-in-polygon results, and app/services/geofencing.py evaluates
polygons with real PostGIS calls (ST_Contains/ST_MakePoint/ST_SetSRID,
plus ST_AsText for the CRUD response) that don't exist in SQLite either.
So GeomFromEWKT/AsEWKB/AsEWKT below round-trip the real EWKT text through
the sqlite column (hex-encoded, so geoalchemy2's WKBElement construction
on read doesn't choke on non-hex text) instead of returning a dummy, and
_SPATIALITE_GIS_STUBS implements genuine (if minimal) versions of the
ST_ functions the geofencing service calls, entirely in Python. Only this
test harness works this way — production always hits real PostGIS.
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
from app.api import devices, onboarding, geofences

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
    ("RecoverGeometryColumn", 5), ("DiscardGeometryColumn", 2),
    ("AddGeometryColumn", -1), ("CreateSpatialIndex", 2), ("DisableSpatialIndex", 2),
]

# AsEWKB/GeomFromEWKT/AsEWKT wrap every Geometry-column bind/read
# (geoalchemy2's bind_expression/column_expression) with the real EWKT text
# (e.g. "SRID=4326;POLYGON((...))"), hex-encoded so it round-trips through
# SQLite's TEXT storage as a string that WKBElement's unhexlify-on-read
# doesn't choke on. A Python None passes through as NULL, unencoded.
def _hex_passthrough(v):
    return None if v is None else v.encode("utf-8").hex()


def _hex_identity(v):
    # AsEWKB/AsEWKT read the same hex text back unchanged — WKBElement's
    # constructor unhexlify()s it, producing meaningless (but harmless,
    # since nothing decodes a WKBElement's .data in this codebase) type/SRID
    # bytes. What matters is the *stored* hex round-trips for the ST_ stubs
    # below, which decode it back into real EWKT text themselves.
    return v


def _ewkt_from_hex(geom_hex):
    """Undo _hex_passthrough and strip any "SRID=...;" prefix, returning
    plain WKT text — e.g. "POLYGON((30.05 -1.9,...))"."""
    ewkt = bytes.fromhex(geom_hex).decode("utf-8")
    return ewkt.split(";", 1)[1] if ewkt.startswith("SRID=") else ewkt


def _polygon_ring_from_wkt(wkt):
    ring_text = wkt[wkt.index("((") + 2 : wkt.rindex("))")]
    return [tuple(map(float, pair.strip().split())) for pair in ring_text.split(",")]


def _point_in_ring(px, py, ring):
    """Even-odd ray-casting point-in-polygon, with an explicit on-edge
    check so boundary points are excluded — matching PostGIS ST_Contains
    semantics (a point exactly on the edge is in neither the interior nor
    the exterior, so containment is false)."""
    n = len(ring)
    for i in range(n - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if _on_segment(px, py, x1, y1, x2, y2):
            return False

    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > py) != (yj > py):
            x_intersect = xi + (py - yi) * (xj - xi) / (yj - yi)
            if px < x_intersect:
                inside = not inside
        j = i
    return inside


def _on_segment(px, py, x1, y1, x2, y2, eps=1e-9):
    cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
    if abs(cross) > eps:
        return False
    return (min(x1, x2) - eps <= px <= max(x1, x2) + eps) and (min(y1, y2) - eps <= py <= max(y1, y2) + eps)


def _st_make_point(lon, lat):
    return f"{lon},{lat}"


def _st_set_srid(point, srid):
    return point  # no reprojection needed for these tests' geometry(...) (not geography) columns


def _st_contains(geom_hex, point):
    if geom_hex is None or point is None:
        return 0
    px, py = (float(v) for v in point.split(","))
    ring = _polygon_ring_from_wkt(_ewkt_from_hex(geom_hex))
    return 1 if _point_in_ring(px, py, ring) else 0


def _st_as_text(geom_hex):
    return None if geom_hex is None else _ewkt_from_hex(geom_hex)


# Real (if minimal) Python implementations of the PostGIS functions
# app/services/geofencing.py and app/api/geofences.py call directly, as
# opposed to the geoalchemy2-internal bind/column wrapping above.
_SPATIALITE_GIS_STUBS = {
    "ST_MakePoint": (2, _st_make_point),
    "ST_SetSRID": (2, _st_set_srid),
    "ST_Contains": (2, _st_contains),
    "ST_AsText": (1, _st_as_text),
}


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
        dbapi_conn.create_function("GeomFromEWKT", 1, _hex_passthrough)
        dbapi_conn.create_function("AsEWKB", 1, _hex_identity)
        dbapi_conn.create_function("AsEWKT", 1, _hex_identity)
        for name, (argc, fn) in _SPATIALITE_GIS_STUBS.items():
            dbapi_conn.create_function(name, argc, fn)

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
    app.include_router(geofences.router, prefix="/api/geofences")

    def _override_get_db():
        yield db_session

    async def _override_require_auth():
        return current_clerk_id["value"]

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_auth] = _override_require_auth

    with TestClient(app) as c:
        yield c

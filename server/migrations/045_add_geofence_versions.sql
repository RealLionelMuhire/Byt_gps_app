-- Migration 045: geofence_versions — time-aware geofence history
--
-- `geofences` only stores a zone's CURRENT state: toggling it off/on,
-- moving it, or reassigning its devices overwrites the old values, and
-- DELETE removes the row. Historical Routes needs to draw only the zones
-- that were actually in effect for a device during a past period (a zone
-- created on the 22nd must not appear on a route from the 21st), so every
-- create/update/delete in app/api/geofences.py now appends a row here.
-- See app/models/geofence_version.py.
--
-- geofence_id is deliberately NOT a foreign key — a deleted zone's history
-- must outlive it.
--
-- BACKFILL: every existing geofence gets one open-ended version starting
-- at its created_at with its current state. Any toggles/edits made before
-- this migration were never recorded anywhere, so existing zones are
-- treated as having been in their current state since creation — a known,
-- one-time approximation.
--
-- Purely additive, touches no existing data. IDEMPOTENT — safe to re-run
-- (the backfill skips geofences that already have a version). Run with:
--   psql $DATABASE_URL -f migrations/045_add_geofence_versions.sql

BEGIN;

CREATE TABLE IF NOT EXISTS geofence_versions (
    id SERIAL PRIMARY KEY,
    geofence_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    shape_type VARCHAR(10) NOT NULL,
    center_latitude DOUBLE PRECISION,
    center_longitude DOUBLE PRECISION,
    radius_meters DOUBLE PRECISION,
    points JSON,
    device_ids JSON NOT NULL DEFAULT '[]',
    is_active BOOLEAN NOT NULL,
    valid_from TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    valid_to TIMESTAMP WITHOUT TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_geofence_versions_user_window
    ON geofence_versions(user_id, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_geofence_versions_geofence_open
    ON geofence_versions(geofence_id, valid_to);

ALTER TABLE geofence_versions ENABLE ROW LEVEL SECURITY;

INSERT INTO geofence_versions (
    geofence_id, user_id, name, shape_type,
    center_latitude, center_longitude, radius_meters,
    points, device_ids, is_active, valid_from, valid_to
)
SELECT
    g.id, g.user_id, g.name, g.shape_type,
    CASE WHEN g.shape_type = 'circle' THEN g.center_latitude END,
    CASE WHEN g.shape_type = 'circle' THEN g.center_longitude END,
    CASE WHEN g.shape_type = 'circle' THEN g.radius_meters END,
    CASE WHEN g.shape_type = 'polygon' AND g.geom IS NOT NULL THEN (
        -- Open ring of {lat, lng}, dropping the closing point that repeats
        -- the first — the same shape _parse_polygon_wkt returns.
        SELECT json_agg(json_build_object('lat', ST_Y(dp.geom), 'lng', ST_X(dp.geom)) ORDER BY dp.path[1])
        FROM ST_DumpPoints(ST_ExteriorRing(g.geom)) AS dp
        WHERE dp.path[1] < ST_NPoints(ST_ExteriorRing(g.geom))
    ) END,
    COALESCE(
        (SELECT json_agg(gd.device_id ORDER BY gd.device_id)
         FROM geofence_devices gd WHERE gd.geofence_id = g.id),
        '[]'::json
    ),
    g.is_active,
    COALESCE(g.created_at, now() AT TIME ZONE 'utc'),
    NULL
FROM geofences g
WHERE NOT EXISTS (SELECT 1 FROM geofence_versions v WHERE v.geofence_id = g.id);

COMMENT ON TABLE geofence_versions IS 'Append-only history of each geofence''s state (shape/active/devices) over [valid_from, valid_to). valid_to NULL = current. geofence_id is not an FK so history survives deletion. Read by GET /api/geofences/history for Historical Routes.';

COMMIT;

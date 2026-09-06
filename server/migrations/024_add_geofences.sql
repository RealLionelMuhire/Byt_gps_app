-- Migration 024: Promote geofences to a real table + add geofence_device_state
--
-- The `geofences` table has existed only implicitly, via init_db()'s
-- Base.metadata.create_all() (see app/models/geofence.py) — never a numbered
-- migration, and never any API around it. It's now load-bearing: server-side
-- geofence evaluation (app/services/geofencing.py) computes enter/exit
-- transitions itself, since neither supported hardware model (TK903ELE,
-- G900LS J16-4G) exposes a command to push a zone definition to the device
-- — see docs/usage/CONFIGURATION_GUIDE.md's Alarms sections.
--
-- This migration:
--   1. Creates `geofences` if it doesn't exist yet (fresh installs that run
--      migrations without ever calling init_db()).
--   2. Adds `user_id` (ownership — v1 CRUD is per-user, not per-device) to an
--      existing `geofences` table that predates this migration. Added
--      straight to NOT NULL: the table has never had a CRUD API, so on any
--      real deployment it has zero rows.
--   3. Relaxes `geom` (polygon) to nullable — v1 geofences are circle-only
--      (center_latitude/longitude/radius_meters); geom is reserved for a
--      future polygon geofence type. A CHECK constraint requires one shape
--      or the other to be present.
--   4. Creates `geofence_device_state`, the per-(device, geofence)
--      inside/outside tracker evaluate_geofences() uses to detect actual
--      transitions instead of re-firing "inside" on every ping.
--
-- IDEMPOTENT — safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS geofences (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    description VARCHAR(500),
    geom geometry(POLYGON, 4326),
    center_latitude DOUBLE PRECISION,
    center_longitude DOUBLE PRECISION,
    radius_meters DOUBLE PRECISION,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    alert_on_enter BOOLEAN NOT NULL DEFAULT TRUE,
    alert_on_exit BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

-- Upgrade path for a `geofences` table that already existed (created by
-- create_all() before this migration) without user_id, and with geom NOT NULL.
ALTER TABLE geofences ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE geofences ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE geofences ALTER COLUMN geom DROP NOT NULL;
ALTER TABLE geofences ALTER COLUMN is_active SET NOT NULL;
ALTER TABLE geofences ALTER COLUMN alert_on_enter SET NOT NULL;
ALTER TABLE geofences ALTER COLUMN alert_on_exit SET NOT NULL;

DO $$
BEGIN
    ALTER TABLE geofences ADD CONSTRAINT geofences_shape_present CHECK (
        geom IS NOT NULL
        OR (center_latitude IS NOT NULL AND center_longitude IS NOT NULL AND radius_meters IS NOT NULL)
    );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_geofences_user_id ON geofences(user_id);

CREATE TABLE IF NOT EXISTS geofence_device_state (
    id SERIAL PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    geofence_id INTEGER NOT NULL REFERENCES geofences(id) ON DELETE CASCADE,
    is_inside BOOLEAN NOT NULL,
    updated_at TIMESTAMP DEFAULT now(),
    UNIQUE (device_id, geofence_id)
);

CREATE INDEX IF NOT EXISTS idx_geofence_device_state_device_id ON geofence_device_state(device_id);
CREATE INDEX IF NOT EXISTS idx_geofence_device_state_geofence_id ON geofence_device_state(geofence_id);

ALTER TABLE geofences ENABLE ROW LEVEL SECURITY;
ALTER TABLE geofence_device_state ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE geofences IS 'User-owned circle geofences (v1), evaluated server-side by app/services/geofencing.py on every incoming location fix.';
COMMENT ON TABLE geofence_device_state IS 'Per-(device, geofence) inside/outside tracker used to detect enter/exit transitions instead of re-firing on every ping.';

COMMIT;

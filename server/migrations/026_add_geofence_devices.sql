-- Migration 026: Add geofence_devices — explicit device scoping for geofences
--
-- Until now, evaluate_geofences() (app/services/geofencing.py) filtered
-- geofences only by Geofence.user_id: a geofence implicitly applied to
-- EVERY device the owning user had, with no way to scope a zone to a
-- subset of vehicles. This migration adds an explicit assignment table.
--
-- Default behavior for a geofence with zero rows here: applies to NO
-- devices, not "all of the owner's devices". That's a deliberate choice —
-- silently treating an empty assignment as "everything" is easy to
-- misread as "nothing configured yet, harmless" when it actually means
-- "fires for the whole fleet". Callers must explicitly assign devices
-- (see app/api/geofences.py's device_ids) before a zone does anything.
--
-- No backfill needed: geofences has zero rows in every environment this
-- has been checked in (see migration 024/025's own comments — the CRUD
-- API is new), so there's no existing "used to apply to everyone" zone
-- that this migration silently narrows.
--
-- IDEMPOTENT — safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS geofence_devices (
    id SERIAL PRIMARY KEY,
    geofence_id INTEGER NOT NULL REFERENCES geofences(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    created_at TIMESTAMP DEFAULT now(),
    UNIQUE (geofence_id, device_id)
);

CREATE INDEX IF NOT EXISTS idx_geofence_devices_geofence_id ON geofence_devices(geofence_id);
CREATE INDEX IF NOT EXISTS idx_geofence_devices_device_id ON geofence_devices(device_id);

ALTER TABLE geofence_devices ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE geofence_devices IS 'Explicit geofence-to-device scoping. A geofence with no rows here applies to no devices, not implicitly to the whole fleet — see app/services/geofencing.py.';

COMMIT;

-- Migration 030: Persist which geofence a synthesized "Enter fence"/"Exit
-- fence" alarm was for.
--
-- app/services/geofencing.py's evaluate_geofences() returns the full
-- Geofence object (with its .name) at the moment a transition fires, but
-- until now app/tcp_server.py's _apply_geofence_transitions only ever
-- persisted the generic alarm_type string ("Enter fence"/"Exit fence") —
-- the specific zone was never recorded, so a past geofence alarm couldn't
-- say which fence it was for.

BEGIN;

ALTER TABLE locations ADD COLUMN IF NOT EXISTS geofence_name VARCHAR(100) NULL;

COMMENT ON COLUMN locations.geofence_name IS 'Snapshot of Geofence.name at the moment this row''s "Enter fence"/"Exit fence" alarm_type fired. NULL for every other alarm_type (or no alarm at all). Not a foreign key deliberately — a historical alarm should keep reporting the zone name as it was then, even if the geofence is later renamed or deleted.';

COMMIT;

-- Migration 029: User-configurable speed limit per device
--
-- Neither supported hardware model (TK903ELE, G900LS J16-4G) exposes a
-- command to set or even read the device's own fixed-firmware "Over speed"
-- alarm threshold (GT06 alarm byte 0x06) — same gap
-- app/services/geofencing.py's docstring already documents for geofence
-- zones. This adds a user-settable alternative, evaluated server-side on
-- every incoming location fix by app/services/speed_limit.py.

BEGIN;

ALTER TABLE devices ADD COLUMN IF NOT EXISTS speed_limit_kmh FLOAT NULL;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS is_overspeeding BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN devices.speed_limit_kmh IS 'Owner-configured speed threshold (km/h) for this device. NULL = no custom limit set (the device''s own fixed-firmware overspeed alarm, if any, is unaffected either way). Set/read via GET/PUT /api/devices/{device_id}/speed_limit.';
COMMENT ON COLUMN devices.is_overspeeding IS 'Edge-detection state for speed_limit_kmh — True while the most recent fix was over the limit, so app/services/speed_limit.py fires a new "Over speed" alarm only on the transition into overspeed, not on every subsequent still-over-limit fix. Purely internal bookkeeping, not read by the app.';

COMMIT;

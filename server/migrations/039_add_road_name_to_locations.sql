-- Migration 039: Persist the reverse-geocoded road name an "Over speed"
-- alarm fired on, mirroring migration 030's geofence_name for fence alarms.
--
-- Until now, app/tcp_server.py's overspeed block resolved a road name (via
-- _resolve_overspeed_road_name, cache-only, never blocking on Nominatim)
-- only for the live WS broadcast / push notification — a historical alarm
-- reopened later in GET /{device_id}/alarms had no way to show it.

BEGIN;

ALTER TABLE locations ADD COLUMN IF NOT EXISTS road_name VARCHAR(200) NULL;

COMMENT ON COLUMN locations.road_name IS 'Reverse-geocoded road name at the moment this row''s "Over speed" alarm_type fired, when the position was already in the geocoding cache at fire time (see _resolve_overspeed_road_name in tcp_server.py). NULL for every other alarm_type, or when the position was not yet cached (the alarm still fired immediately with a coordinate fallback rather than waiting on Nominatim). Not a foreign key -- there is nothing to key against, this is a point-in-time geocoding result.';

COMMIT;

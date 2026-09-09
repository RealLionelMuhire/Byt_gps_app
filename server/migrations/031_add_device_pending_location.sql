-- Migration 031: Debounce state for the live position shown on the map
--
-- device.last_latitude/last_longitude previously updated unconditionally on
-- every incoming ping, including gps_valid=false fixes and single-point
-- reconnect/reacquisition transients (confirmed via TCP logs: a device
-- reconnect resets GPS lock and reports a ~15-25m-shifted position for a
-- parked vehicle before settling back down). app/services/live_position.py
-- now requires a second, corroborating point before trusting a jump away
-- from the current confirmed position while the device reports itself as
-- stopped — these columns hold that not-yet-confirmed candidate between
-- pings (a single TCP connection's in-memory state doesn't survive the
-- reconnect that triggers the problem in the first place, so this has to be
-- persisted on the device row, not kept on the connection handler).

BEGIN;

ALTER TABLE devices ADD COLUMN IF NOT EXISTS pending_latitude FLOAT NULL;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS pending_longitude FLOAT NULL;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS pending_since TIMESTAMP NULL;

COMMENT ON COLUMN devices.pending_latitude IS 'Unconfirmed live-position candidate awaiting a corroborating next point (see app/services/live_position.py). NULL when there is no point currently being held.';
COMMENT ON COLUMN devices.pending_longitude IS 'Paired with pending_latitude.';
COMMENT ON COLUMN devices.pending_since IS 'When the pending candidate was staged — a candidate older than LIVE_POSITION_PENDING_MAX_AGE_SECONDS is discarded rather than confirmed, so a coincidental later point near a stale candidate can''t confirm it.';

COMMIT;

-- Migration 040: Debounce state for geofence enter/exit transitions
--
-- evaluate_geofences previously flipped GeofenceDeviceState.is_inside (and
-- fired a transition) on ANY single fix that disagreed with the stored
-- state, with no buffer or corroboration -- unlike the live position shown
-- on the map (migration 031), which already requires a second,
-- corroborating point before trusting a jump while the device reports
-- itself stopped. A stationary vehicle's raw GPS fix can still wobble a
-- few meters ping to ping even within the "trusted" jitter envelope
-- app/services/live_position.py confirms immediately (CONFIRM_RADIUS_METERS
-- = 8m, tuned for the live map marker not visibly jumping around) -- if a
-- geofence boundary happens to sit within that noise, the confirmed
-- position itself can flip sides ping to ping, and with no debounce of its
-- own, each flip fired a real "Enter fence"/"Exit fence" alarm for a
-- vehicle that never moved. This column holds a candidate flip until a
-- second consecutive fix agrees with it before it's committed and a
-- transition fires -- same pending-then-confirmed shape as
-- devices.pending_latitude/longitude (migration 031), just applied to the
-- boolean is_inside signal instead of a lat/lon jump.

BEGIN;

ALTER TABLE geofence_device_state ADD COLUMN IF NOT EXISTS pending_is_inside BOOLEAN NULL;

COMMENT ON COLUMN geofence_device_state.pending_is_inside IS 'A candidate is_inside flip awaiting a second, corroborating fix before it is committed and a transition fires (see app/services/geofencing.py). NULL when there is no candidate currently being held, or once the previously-committed value is seen again (the candidate is cleared, not left stale).';

COMMIT;

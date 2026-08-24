-- Migration 016: Add is_outlier flag to locations
--
-- Flags GPS fixes that fail an implied-speed plausibility check against
-- their neighbors (e.g. a single bad fix causing a large jump-and-return
-- between 2-3 points). Points are flagged, never deleted, so raw
-- history/diagnostics stay intact — /history returns everything as before,
-- while /route, distance, and trip-detection queries filter is_outlier=false.

ALTER TABLE locations ADD COLUMN IF NOT EXISTS is_outlier BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN locations.is_outlier IS 'Flagged as an implausible GPS jump (implied speed exceeds MAX_PLAUSIBLE_SPEED_KMH in app/api/locations.py); excluded from route/distance/trip queries by default but retained for audit. Never deletes rows.';

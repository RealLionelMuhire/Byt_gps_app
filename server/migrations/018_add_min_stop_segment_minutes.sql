-- Migration 018: Add min_stop_segment_minutes to trip_settings
--
-- Minimum duration (minutes) a low-speed run must last to be reported as
-- its own "stopped" segment in the period-route endpoint (GET
-- /api/locations/{device_id}/period-route). Shorter dips below
-- stop_speed_threshold_kmh (e.g. a red light) are folded into the
-- surrounding "driving" segment instead of fragmenting it. Distinct from
-- stop_splits_trip_after_minutes, which governs when a stop is long enough
-- to split into a separate trip.

ALTER TABLE trip_settings ADD COLUMN IF NOT EXISTS min_stop_segment_minutes INTEGER NOT NULL DEFAULT 10;

COMMENT ON COLUMN trip_settings.min_stop_segment_minutes IS 'Minimum duration (minutes) a low-speed run must last to be reported as its own "stopped" segment in period-route; shorter dips are folded into the surrounding driving segment.';

-- Migration 017: Add location_quality_log table
--
-- Per-point GPS quality diagnostics for ongoing tuning and audit: satellites,
-- implied speed vs. the previous point, direction-consistency (reported
-- course vs. movement bearing), reporting gap, and whether the point was
-- flagged as an outlier or a time-gap segment break. One row per location
-- (1:1 via location_id), independent of the locations table itself.

CREATE TABLE IF NOT EXISTS location_quality_log (
    id SERIAL PRIMARY KEY,
    location_id INTEGER NOT NULL UNIQUE REFERENCES locations(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    timestamp TIMESTAMP NOT NULL,
    satellites INTEGER NOT NULL,
    implied_speed_kmh DOUBLE PRECISION,
    course_delta_degrees DOUBLE PRECISION,
    gap_seconds DOUBLE PRECISION,
    is_outlier BOOLEAN NOT NULL DEFAULT FALSE,
    is_segment_break BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_location_quality_log_device_timestamp
    ON location_quality_log (device_id, timestamp);

COMMENT ON TABLE location_quality_log IS 'Per-point GPS quality diagnostics (satellites, implied speed, direction consistency, outlier/segment-break flags) for ongoing tuning and audit. Populated at ingestion (tcp_server.py) and by scripts/backfill_location_quality_log.py for existing rows.';

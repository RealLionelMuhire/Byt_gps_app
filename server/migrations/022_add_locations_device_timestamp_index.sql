-- Migration 022: Composite index on locations(device_id, timestamp)
--
-- Every location-history read (period-route, route, route-line, trips,
-- since-last-stop, history) filters by device_id and a timestamp range,
-- ordered by timestamp. Only single-column indexes exist today
-- (ix_locations_device_id, ix_locations_timestamp), forcing Postgres to
-- either bitmap-AND the two or fall back to filtering/sorting in memory.
-- A composite index lets it satisfy the equality + range + order-by in one
-- index scan, with no query-result change -- purely additive/safe.

BEGIN;

CREATE INDEX IF NOT EXISTS ix_locations_device_id_timestamp
    ON locations (device_id, timestamp);

COMMENT ON INDEX ix_locations_device_id_timestamp IS 'Speeds up every device_id + timestamp-range location-history query (period-route, route, trips, since-last-stop, history) -- see migration 022.';

COMMIT;

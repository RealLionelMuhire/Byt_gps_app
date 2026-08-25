-- Migration 019: Add geocode_cache table
--
-- Persists Nominatim reverse-geocoding results keyed by lat/lon rounded to
-- 3 decimal places (~100m precision), so repeated lookups for the same
-- location (period-route driving-segment endpoints, trip display names)
-- don't re-hit Nominatim. Respects Nominatim's fair-use policy (max 1
-- req/sec, avoid heavy repeated querying of the same locations) and
-- survives process restarts / multiple workers, unlike the in-process-only
-- cache it backs (see app/services/geocoding.py).

CREATE TABLE IF NOT EXISTS geocode_cache (
    id SERIAL PRIMARY KEY,
    lat FLOAT NOT NULL,
    lon FLOAT NOT NULL,
    place_name VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_geocode_cache_lat_lon UNIQUE (lat, lon)
);

COMMENT ON TABLE geocode_cache IS 'Persistent cache of successful Nominatim reverse-geocoding results, keyed by lat/lon rounded to 3 decimals (~100m). Failed/no-result lookups are not persisted here.';

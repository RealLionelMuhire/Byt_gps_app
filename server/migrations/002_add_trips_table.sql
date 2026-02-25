-- Migration: Add trips table for saved trips
-- Run this on existing databases to add trip support
-- Date: 2026-02-25

BEGIN;

-- ============================================================================
-- 1. CREATE TRIPS TABLE
-- ============================================================================

CREATE TABLE IF NOT EXISTS trips (
    id SERIAL PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP NOT NULL,
    total_distance_km FLOAT NOT NULL DEFAULT 0.0,
    start_location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    end_location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_trips_device_id ON trips(device_id);
CREATE INDEX IF NOT EXISTS idx_trips_user_id ON trips(user_id);
CREATE INDEX IF NOT EXISTS idx_trips_created_at ON trips(created_at DESC);

COMMIT;

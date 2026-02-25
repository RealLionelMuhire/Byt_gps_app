-- Migration: Add trip_settings table for user trip segmentation preferences
-- Date: 2026-02-25

BEGIN;

CREATE TABLE IF NOT EXISTS trip_settings (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    stop_splits_trip_after_minutes INTEGER NOT NULL DEFAULT 60,
    minimum_trip_duration_minutes INTEGER NOT NULL DEFAULT 5,
    stop_speed_threshold_kmh FLOAT NOT NULL DEFAULT 5.0
);

CREATE INDEX IF NOT EXISTS idx_trip_settings_user_id ON trip_settings(user_id);

COMMIT;

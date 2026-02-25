-- Migration: Add display_name to trips table (reverse geocoding)
-- Run this on existing databases to add human-readable trip names
-- Date: 2026-02-25

BEGIN;

ALTER TABLE trips
  ADD COLUMN IF NOT EXISTS display_name VARCHAR(512);

COMMIT;

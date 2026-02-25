-- Migration: Make trip end_time nullable for active trips (auto-ended when device stops)
-- Date: 2026-02-25

BEGIN;

ALTER TABLE trips
  ALTER COLUMN end_time DROP NOT NULL;

COMMIT;

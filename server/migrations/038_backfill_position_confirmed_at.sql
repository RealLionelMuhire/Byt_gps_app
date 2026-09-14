-- Migration 038: Backfill position_confirmed_at for devices that already
-- have a confirmed position from before migration 037 added the column.
-- Run with: psql $DATABASE_URL -f migrations/038_backfill_position_confirmed_at.sql
--
-- Without this, a device's position_confirmed_at stays NULL until its next
-- gps_valid fix — which never comes for a device already stuck exactly in
-- the "GPS off, only invalid pings" state this feature exists to surface,
-- silently defeating it for the one case that matters most. Backfills from
-- each device's own most recent valid Location row, an accurate proxy for
-- when last_latitude/last_longitude were actually last confirmed.

UPDATE devices d
SET position_confirmed_at = (
    SELECT MAX(l.timestamp)
    FROM locations l
    WHERE l.device_id = d.id AND l.gps_valid = true
)
WHERE d.position_confirmed_at IS NULL
  AND d.last_latitude IS NOT NULL
  AND d.last_longitude IS NOT NULL;

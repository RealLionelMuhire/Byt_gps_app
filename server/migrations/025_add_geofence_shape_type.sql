-- Migration 025: Add shape_type to geofences (circle vs polygon)
--
-- geofences.geom (PostGIS POLYGON) has existed since migration 024 but was
-- never evaluated — v1 geofencing (app/services/geofencing.py) only checked
-- circle fields (center_latitude/longitude/radius_meters). This migration
-- adds `shape_type` so a row can declare which shape it actually is, and
-- tightens the old "some shape is present" CHECK into "the *declared*
-- shape's fields are present" now that both shapes are evaluated.
--
-- Backfill: any pre-existing row is classified by what it already has
-- populated (geom set => polygon, else => circle) — matches how those rows
-- were being interpreted before this migration existed.
--
-- IDEMPOTENT — safe to re-run.

BEGIN;

ALTER TABLE geofences ADD COLUMN IF NOT EXISTS shape_type VARCHAR(10);

UPDATE geofences
SET shape_type = CASE WHEN geom IS NOT NULL THEN 'polygon' ELSE 'circle' END
WHERE shape_type IS NULL;

ALTER TABLE geofences ALTER COLUMN shape_type SET DEFAULT 'circle';
ALTER TABLE geofences ALTER COLUMN shape_type SET NOT NULL;

DO $$
BEGIN
    ALTER TABLE geofences DROP CONSTRAINT geofences_shape_present;
EXCEPTION
    WHEN undefined_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE geofences ADD CONSTRAINT geofences_shape_matches_type CHECK (
        (shape_type = 'circle' AND center_latitude IS NOT NULL AND center_longitude IS NOT NULL AND radius_meters IS NOT NULL)
        OR
        (shape_type = 'polygon' AND geom IS NOT NULL)
    );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

COMMENT ON COLUMN geofences.shape_type IS 'Which of the two mutually-exclusive shapes this row uses: "circle" (center_latitude/longitude/radius_meters) or "polygon" (geom).';

COMMIT;

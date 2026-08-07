-- Migration 013: Add marker_icon to devices
-- Lets each device pick which glyph its map marker renders as (the
-- Flutter client's directional-arrow rotation logic still applies on top
-- of whichever icon this selects — this only chooses the base image, e.g.
-- a sedan/truck/bus silhouette instead of the default arrow).

ALTER TABLE devices ADD COLUMN IF NOT EXISTS marker_icon VARCHAR(20) NOT NULL DEFAULT 'arrow';

COMMENT ON COLUMN devices.marker_icon IS 'Map marker icon: arrow (default) | sedan | truck_small | truck_big | bus | animal';

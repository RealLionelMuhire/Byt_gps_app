-- Migration 015: subscription model fix + device lifecycle/status constraints
--
-- 1. Add `updated_at` and `price` columns to the `subscriptions` table
--    (price is stored at purchase time so admin edits to plan prices never
--     affect existing subscribers).
-- 2. Add CHECK constraints to the `devices` table for lifecycle, status,
--    and marker_icon so invalid values are rejected at the DB level.

-- ── 1. Subscriptions ─────────────────────────────────────────────────────────

ALTER TABLE subscriptions
  ADD COLUMN IF NOT EXISTS price       FLOAT NOT NULL DEFAULT 0.0,
  ADD COLUMN IF NOT EXISTS updated_at  TIMESTAMP DEFAULT NOW();

-- Back-fill price for existing subscriptions from the subscription_plans table
-- (or fallback values when the plan row has been deleted).
UPDATE subscriptions
  SET price = COALESCE(
    (SELECT sp.price FROM subscription_plans sp WHERE sp.slug = subscriptions.plan_id),
    CASE
      WHEN subscriptions.plan_id = 'trial' THEN 0
      WHEN subscriptions.plan_id = 'basic' THEN 5000
      WHEN subscriptions.plan_id = 'fleet' THEN 15000
      ELSE 0
    END
  )
  WHERE price = 0.0;


-- ── 2. Device CHECK constraints ──────────────────────────────────────────────

-- lifecycle must be one of the three valid states
ALTER TABLE devices
  DROP CONSTRAINT IF EXISTS chk_device_lifecycle,
  ADD CONSTRAINT chk_device_lifecycle
    CHECK (lifecycle IN ('registered', 'in_stock', 'sold'));

-- status must be online or offline
ALTER TABLE devices
  DROP CONSTRAINT IF EXISTS chk_device_status,
  ADD CONSTRAINT chk_device_status
    CHECK (status IN ('online', 'offline'));

-- marker_icon must be one of the known glyphs
ALTER TABLE devices
  DROP CONSTRAINT IF EXISTS chk_device_marker_icon,
  ADD CONSTRAINT chk_device_marker_icon
    CHECK (marker_icon IN ('arrow', 'sedan', 'truck_small', 'truck_big', 'bus', 'animal'));
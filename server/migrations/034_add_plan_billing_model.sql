-- Migration 034: plan billing model (prepaid/postpaid) + charge scope (per_device/flat)
--
-- 1. `billing_model` — 'prepaid' (pay up-front — the current behaviour) | 'postpaid'
-- 2. `charge_scope`  — 'per_device' (price applies to each vehicle) | 'flat' (price
--                      covers all of the subscriber's vehicles)
--
-- Existing plans are priced once per subscription regardless of device count,
-- so they back-fill as prepaid + flat: the NOT NULL DEFAULT on ADD COLUMN
-- populates every existing row automatically.
-- Run with: psql $DATABASE_URL -f migrations/034_add_plan_billing_model.sql

ALTER TABLE subscription_plans
  ADD COLUMN IF NOT EXISTS billing_model VARCHAR(20) NOT NULL DEFAULT 'prepaid',
  ADD COLUMN IF NOT EXISTS charge_scope   VARCHAR(20) NOT NULL DEFAULT 'flat';

ALTER TABLE subscription_plans
  DROP CONSTRAINT IF EXISTS chk_subscription_plans_billing_model,
  ADD CONSTRAINT chk_subscription_plans_billing_model
    CHECK (billing_model IN ('prepaid', 'postpaid'));

ALTER TABLE subscription_plans
  DROP CONSTRAINT IF EXISTS chk_subscription_plans_charge_scope,
  ADD CONSTRAINT chk_subscription_plans_charge_scope
    CHECK (charge_scope IN ('per_device', 'flat'));

COMMENT ON COLUMN subscription_plans.billing_model IS
  'prepaid = pay before the period starts | postpaid = billed after the period';
COMMENT ON COLUMN subscription_plans.charge_scope IS
  'per_device = price applies to each vehicle | flat = price covers all of the subscriber''s vehicles';
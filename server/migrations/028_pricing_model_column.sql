-- Migration 028: Add pricing_model to subscription_plans
-- pricing_model: 'per_device' (price × number of devices) or 'flat' (single price covers all)

ALTER TABLE subscription_plans
ADD COLUMN IF NOT EXISTS pricing_model VARCHAR(20) NOT NULL DEFAULT 'flat';

-- Set sensible defaults for existing plans
UPDATE subscription_plans SET pricing_model = 'per_device' WHERE slug = 'basic';
UPDATE subscription_plans SET pricing_model = 'flat' WHERE slug IN ('trial', 'fleet');

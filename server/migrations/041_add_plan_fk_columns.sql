-- Migration 041: FK-ify subscriptions.plan_id and payments.plan_id (step 1/2)
--
-- Both columns currently store a bare plan SLUG string (see
-- app/models/subscription.py's history), not a foreign key to
-- subscription_plans.id — a renamed/re-slugged plan silently orphans every
-- historical row referencing its old slug, and nothing in the schema catches
-- a typo'd or stale slug either. This step is purely additive and 100% safe
-- to run against production as-is:
--   1. Adds a new nullable FK column next to each existing string column.
--   2. Auto-creates a subscription_plans row for the three legacy hardcoded
--      slugs (see app/api/subscriptions.py's FALLBACK_PLANS) if one doesn't
--      already exist, so accounts created before a real "trial"/"basic"/
--      "fleet" DB row existed still resolve cleanly.
--   3. Backfills the new column by matching each row's existing slug
--      (case-insensitively) against subscription_plans.slug.
--   4. Reports (via NOTICE) any row whose slug still doesn't match anything.
--
-- Nothing reads or writes the new columns yet, and the existing plan_id
-- string columns are untouched — the application keeps working exactly as
-- before after this runs. Migration 042 is the separate, second step that
-- actually swaps the columns over — it must not run until every row here
-- resolves cleanly, and it will refuse to run otherwise. Do NOT deploy the
-- application code that expects Subscription.plan_id/Payment.plan_id to be
-- integers (see app/models/subscription.py) until AFTER migration 042 has
-- completed — the ORM's column type won't match the DB schema until then.
--
-- Run with: psql $DATABASE_URL -f migrations/041_add_plan_fk_columns.sql
--
-- AFTER RUNNING: inspect the NOTICE output at the end for "unresolved slug"
-- warnings before EVER running migration 042. Each one names a real
-- historical plan_id value with no matching subscription_plans row — likely
-- a plan that was renamed/re-slugged (its old slug no longer exists
-- anywhere) or a typo. These need a human decision — rename the row's
-- plan_id to the plan's current slug, or create a subscription_plans row for
-- the old slug — this migration deliberately does not guess. This script
-- cannot verify your production data for you; the author of this migration
-- has not run it against production and cannot confirm production is free
-- of renamed/re-slugged plans.
--
-- Idempotent — safe to re-run (e.g. after creating a subscription_plans row
-- for a previously-unresolved slug, to pick it up without redoing anything
-- already resolved).

ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS plan_ref_id INTEGER REFERENCES subscription_plans(id);
ALTER TABLE payments      ADD COLUMN IF NOT EXISTS plan_ref_id INTEGER REFERENCES subscription_plans(id);

-- Auto-create the three legacy hardcoded plans (FALLBACK_PLANS in
-- app/api/subscriptions.py) if no subscription_plans row uses that slug yet.
-- Values match FALLBACK_PLANS exactly; billing_type/billing_model/charge_scope
-- default to the same values every real seeded plan uses.
INSERT INTO subscription_plans (name, slug, billing_type, billing_model, charge_scope, price, currency, duration_value, duration_unit, max_devices, is_active, created_at, updated_at)
SELECT 'Trial', 'trial', 'recurrent', 'prepaid', 'flat', 0, 'RWF', 14, 'day', 1, true, now(), now()
WHERE NOT EXISTS (SELECT 1 FROM subscription_plans WHERE LOWER(slug) = 'trial');

INSERT INTO subscription_plans (name, slug, billing_type, billing_model, charge_scope, price, currency, duration_value, duration_unit, max_devices, is_active, created_at, updated_at)
SELECT 'Basic', 'basic', 'recurrent', 'prepaid', 'flat', 2450, 'RWF', 1, 'month', 3, true, now(), now()
WHERE NOT EXISTS (SELECT 1 FROM subscription_plans WHERE LOWER(slug) = 'basic');

INSERT INTO subscription_plans (name, slug, billing_type, billing_model, charge_scope, price, currency, duration_value, duration_unit, max_devices, is_active, created_at, updated_at)
SELECT 'Fleet', 'fleet', 'recurrent', 'prepaid', 'flat', 15000, 'RWF', 1, 'month', NULL, true, now(), now()
WHERE NOT EXISTS (SELECT 1 FROM subscription_plans WHERE LOWER(slug) = 'fleet');

-- Backfill: match each row's existing slug string against subscription_plans
-- case-insensitively. Re-runnable — only touches rows not yet resolved.
UPDATE subscriptions s
SET plan_ref_id = p.id
FROM subscription_plans p
WHERE s.plan_ref_id IS NULL
  AND LOWER(p.slug) = LOWER(s.plan_id);

UPDATE payments pay
SET plan_ref_id = p.id
FROM subscription_plans p
WHERE pay.plan_ref_id IS NULL
  AND LOWER(p.slug) = LOWER(pay.plan_id);

-- Report anything still unresolved — a real renamed/re-slugged/typo'd plan
-- reference migration 042 must not silently drop or misassign.
DO $$
DECLARE
    unresolved_subs INTEGER;
    unresolved_payments INTEGER;
    sample TEXT;
BEGIN
    SELECT count(*) INTO unresolved_subs FROM subscriptions WHERE plan_ref_id IS NULL;
    SELECT count(*) INTO unresolved_payments FROM payments WHERE plan_ref_id IS NULL;

    IF unresolved_subs > 0 THEN
        SELECT string_agg(DISTINCT plan_id, ', ') INTO sample
        FROM subscriptions WHERE plan_ref_id IS NULL;
        RAISE NOTICE 'subscriptions: % row(s) with an unresolved plan_id — distinct values: %', unresolved_subs, sample;
    END IF;

    IF unresolved_payments > 0 THEN
        SELECT string_agg(DISTINCT plan_id, ', ') INTO sample
        FROM payments WHERE plan_ref_id IS NULL;
        RAISE NOTICE 'payments: % row(s) with an unresolved plan_id — distinct values: %', unresolved_payments, sample;
    END IF;

    IF unresolved_subs = 0 AND unresolved_payments = 0 THEN
        RAISE NOTICE 'All subscriptions/payments rows resolved to a subscription_plans row — safe to run migration 042.';
    END IF;
END $$;

-- Migration 044: fix a bug in migration 042 — plan_slug_legacy still NOT NULL
--
-- Migration 042 renamed subscriptions.plan_id / payments.plan_id (the old
-- bare-slug-string columns, originally NOT NULL) to plan_slug_legacy, and
-- its own COMMENT ON COLUMN for both says "kept for audit only — the
-- application does not read this column." But it never dropped the NOT
-- NULL constraint on that renamed column, and app/models/subscription.py's
-- Payment/Subscription ORM models don't map plan_slug_legacy at all (only
-- the new integer plan_id FK) — so every INSERT since 042 was applied omits
-- it, and Postgres rejects the row with a NotNullViolation. Confirmed live
-- 2026-09-22: POST /api/payments/initiate failing 500 with exactly this
-- error, both a new /initiate call and a later retry after IntouchPay's
-- webhook came back "successfull" for a tx_ref that was never actually
-- saved to the payments table in the first place.
--
-- Purely additive/permissive — only relaxes a constraint, touches no data,
-- drops nothing. Safe to run against production as-is.
--
-- Run with: psql $DATABASE_URL -f migrations/044_fix_plan_slug_legacy_not_null.sql

BEGIN;

ALTER TABLE payments      ALTER COLUMN plan_slug_legacy DROP NOT NULL;
ALTER TABLE subscriptions ALTER COLUMN plan_slug_legacy DROP NOT NULL;

COMMIT;

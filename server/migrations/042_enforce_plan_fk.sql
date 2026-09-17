-- Migration 042: FK-ify subscriptions.plan_id and payments.plan_id (step 2/2)
--
-- Finishes what migration 041 started: swaps the bare-slug-string plan_id
-- columns for the real integer foreign keys to subscription_plans.id that
-- 041 backfilled into plan_ref_id.
--
-- REFUSES TO RUN (raises and aborts, no partial changes) if any row's
-- plan_ref_id is still NULL. Do not run this until migration 041's NOTICE
-- output reports zero unresolved rows for BOTH tables, or you will lose the
-- ability to tell which plan an unresolved row was ever on — this migration
-- drops the slug string column outright, it does not keep it around for
-- rows it can't resolve. If you see this abort, go back to migration 041's
-- NOTICE output, resolve every named slug (rename the row's plan_id to the
-- plan's current slug, or create a subscription_plans row for the old slug),
-- re-run migration 041, and only then retry this one.
--
-- This migration has NOT been run against production data by the author —
-- there is no way to confirm from this environment whether production has
-- any renamed/re-slugged/typo'd plan_id values. The abort-on-unresolved-rows
-- behavior above is the actual safety net; do not skip straight to running
-- this without first reading migration 041's NOTICE output for your own
-- database.
--
-- AFTER THIS RUNS: Subscription.plan_id / Payment.plan_id are real integer
-- FKs, not slug strings, and app/models/subscription.py's SQLAlchemy model
-- already reflects that (Column(Integer, ForeignKey("subscription_plans.id"))
-- plus a `plan` relationship) — that code change and this migration must be
-- deployed together. The API layer is unaffected: every endpoint already
-- resolves a subscription/payment's plan through the `plan` relationship and
-- serializes its `.slug`, never the raw column, so no client-facing JSON
-- shape changes for the mobile app or admin portal.
--
-- Run with: psql $DATABASE_URL -f migrations/042_enforce_plan_fk.sql
--
-- Everything below runs in ONE transaction: psql's default is
-- autocommit-per-statement, so a bare `DO $$ RAISE EXCEPTION ... $$` guard
-- (as an earlier draft of this migration had) only aborts that one
-- statement — psql happily continues on to the ALTER TABLE RENAME/SET NOT
-- NULL statements that follow, which is exactly the silent-corruption
-- failure mode this guard exists to prevent. Confirmed by actually running
-- that draft against a real Postgres instance: the RENAMEs went through
-- despite the "abort", and only the final SET NOT NULL happened to fail
-- (because a real orphaned row existed) — that failure was luck, not the
-- guard working. Wrapping in BEGIN/COMMIT makes the abort real: any
-- exception before COMMIT rolls back every statement in this file.

BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM subscriptions WHERE plan_ref_id IS NULL) THEN
        RAISE EXCEPTION 'Aborting: % subscriptions row(s) still have no resolved plan_ref_id — re-run migration 041 after resolving its NOTICE warnings first.',
            (SELECT count(*) FROM subscriptions WHERE plan_ref_id IS NULL);
    END IF;

    IF EXISTS (SELECT 1 FROM payments WHERE plan_ref_id IS NULL) THEN
        RAISE EXCEPTION 'Aborting: % payments row(s) still have no resolved plan_ref_id — re-run migration 041 after resolving its NOTICE warnings first.',
            (SELECT count(*) FROM payments WHERE plan_ref_id IS NULL);
    END IF;
END $$;

-- Rename the old slug columns aside (kept, not dropped — cheap insurance,
-- and lets a human eyeball old-slug-vs-new-id agreement after the fact).
ALTER TABLE subscriptions RENAME COLUMN plan_id TO plan_slug_legacy;
ALTER TABLE payments      RENAME COLUMN plan_id TO plan_slug_legacy;

ALTER TABLE subscriptions RENAME COLUMN plan_ref_id TO plan_id;
ALTER TABLE payments      RENAME COLUMN plan_ref_id TO plan_id;

ALTER TABLE subscriptions ALTER COLUMN plan_id SET NOT NULL;
ALTER TABLE payments      ALTER COLUMN plan_id SET NOT NULL;

-- app/models/subscription.py declares plan_id with index=True (a fresh
-- install via Base.metadata.create_all() gets this automatically) — a FK
-- column isn't auto-indexed by Postgres, so a migrated database needs it
-- created explicitly to match.
CREATE INDEX IF NOT EXISTS ix_subscriptions_plan_id ON subscriptions (plan_id);
CREATE INDEX IF NOT EXISTS ix_payments_plan_id ON payments (plan_id);

COMMENT ON COLUMN subscriptions.plan_id IS 'FK to subscription_plans.id (was a bare slug string — see plan_slug_legacy and migrations 041/042)';
COMMENT ON COLUMN payments.plan_id IS 'FK to subscription_plans.id (was a bare slug string — see plan_slug_legacy and migrations 041/042)';
COMMENT ON COLUMN subscriptions.plan_slug_legacy IS 'Pre-migration-042 slug string, kept for audit only — the application does not read this column.';
COMMENT ON COLUMN payments.plan_slug_legacy IS 'Pre-migration-042 slug string, kept for audit only — the application does not read this column.';

COMMIT;

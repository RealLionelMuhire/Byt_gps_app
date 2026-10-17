-- Migration 054: enable Row Level Security on the public tables that lacked it
--
-- Supabase exposes the `public` schema through its REST API to the `anon`
-- and `authenticated` roles. With RLS disabled, anyone holding the
-- project's (public) anon key could read these tables directly — including
-- disbursements (payout phone numbers/amounts), contact_requests and
-- command_log. Supabase's Security Advisor flagged each one by email every
-- time a migration added a table ("RLS disabled in public"), which is what
-- prompted this (2026-10-18).
--
-- Enabling RLS with NO policies denies anon/authenticated entirely. The
-- backend is unaffected: it connects as `postgres`, which has BYPASSRLS
-- (checked: rolbypassrls = true). Nothing in any Bythron repo uses a
-- Supabase client against these tables.
--
-- RULE FOR EVERY FUTURE MIGRATION THAT CREATES A TABLE: add
--   ALTER TABLE <name> ENABLE ROW LEVEL SECURITY;
-- in the same migration, or the advisor emails (and the exposure) return.
--
-- Not covered: public.spatial_ref_sys belongs to the PostGIS extension and
-- is owned by supabase_admin, so it can't be altered from here; its content
-- is public reference data (coordinate systems), not customer data.
--
-- IDEMPOTENT — safe to re-run:
--   psql $DATABASE_URL -f migrations/054_enable_rls_on_unprotected_tables.sql

BEGIN;

ALTER TABLE command_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE contact_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE disbursements ENABLE ROW LEVEL SECURITY;

COMMIT;

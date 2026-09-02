-- Migration 023: Enable Row Level Security on all public tables
--
-- Supabase's Security Advisor / database linter flags every table in the
-- public schema that doesn't have RLS enabled, because Supabase auto-exposes
-- public tables via its PostgREST REST API — any request carrying the
-- project's anon/service key can read/write a table with RLS off, entirely
-- bypassing this app's own auth checks.
--
-- This app never uses that REST API: every DB access path (HTTP API,
-- WebSocket, GPS device TCP ingestion, geocoding service) goes through the
-- single SessionLocal/engine in app/core/database.py, connecting as the
-- `postgres` role — the same role that owns every table here (it ran every
-- CREATE TABLE). Postgres always lets a table's owner bypass RLS regardless
-- of policies, so enabling RLS with NO policies locks PostgREST out
-- completely while leaving the app's own DB access unaffected.
--
-- spatial_ref_sys is the one exception: it's PostGIS's own reference-data
-- table (not app data), so instead of a blanket lock it gets RLS enabled
-- plus an explicit public read policy.
--
-- IDEMPOTENT — safe to re-run: ENABLE ROW LEVEL SECURITY is a no-op if
-- already enabled; the policy create is guarded against "already exists".

BEGIN;

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscription_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments ENABLE ROW LEVEL SECURITY;
ALTER TABLE trips ENABLE ROW LEVEL SECURITY;
ALTER TABLE trip_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE vehicles ENABLE ROW LEVEL SECURITY;
ALTER TABLE locations ENABLE ROW LEVEL SECURITY;
ALTER TABLE location_quality_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE geocode_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE geofences ENABLE ROW LEVEL SECURITY;
ALTER TABLE command_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;

-- spatial_ref_sys is owned by the postgis extension's install role, not
-- `postgres` — this connecting role isn't its owner, so ALTER/CREATE POLICY
-- on it fails with insufficient_privilege. Best-effort only: skip it rather
-- than aborting the whole migration over a non-app reference table.
DO $$
BEGIN
    ALTER TABLE spatial_ref_sys ENABLE ROW LEVEL SECURITY;
    CREATE POLICY "Allow public read access" ON spatial_ref_sys
        FOR SELECT USING (true);
EXCEPTION
    WHEN duplicate_object THEN NULL;
    WHEN insufficient_privilege THEN
        RAISE NOTICE 'Skipping spatial_ref_sys: % is not the table owner (needs to be run by a project owner/admin in the Supabase SQL Editor instead)', current_user;
END $$;

COMMIT;

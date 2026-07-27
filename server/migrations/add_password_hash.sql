-- Migration: Add password_hash column to users table
-- Run this on the production database to support custom JWT auth.
--
-- This migration is safe to re-run (uses IF NOT EXISTS).
-- Existing Clerk-synced users will have password_hash = NULL;
-- they must register again with a password via POST /api/auth/register.

ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash VARCHAR(255);

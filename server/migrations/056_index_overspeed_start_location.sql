-- Migration 056: index overspeed_episodes.start_location_id
--
-- Migration 055 gave start_location_id an ON DELETE SET NULL foreign key
-- to locations(id) without an index. Postgres then has to scan
-- overspeed_episodes for every row deleted from `locations` — and
-- app/services/account_deletion.py bulk-deletes a user's entire location
-- history, so that check would run once per deleted fix. alarm_location_id
-- already has its index (055); this adds the missing one.
--
-- Purely additive. IDEMPOTENT — safe to re-run:
--   psql $DATABASE_URL -f migrations/056_index_overspeed_start_location.sql

BEGIN;

CREATE INDEX IF NOT EXISTS ix_overspeed_episodes_start_location ON overspeed_episodes (start_location_id);

COMMIT;

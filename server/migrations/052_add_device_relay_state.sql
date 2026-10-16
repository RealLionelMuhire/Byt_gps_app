-- Migration 052: devices.relay_cut / devices.relay_state_at
--
-- The tracker's OWN report of its fuel/relay output, taken from bit 7 of
-- the GT06 terminal-information byte ("oil/electricity disconnected") that
-- every heartbeat (0x13) and alarm (0x16) packet carries. Before this, the
-- bit was decoded in app/protocol_parser.py and then dropped, so no client
-- could tell whether a vehicle was actually immobilized — the apps only
-- remembered the last button pressed in the current session.
--
--   relay_cut      NULL  = never reported (e.g. no heartbeat since deploy)
--                  TRUE  = fuel/relay cut (vehicle immobilized)
--                  FALSE = fuel/relay connected
--   relay_state_at UTC time the tracker last reported relay_cut
--
-- Purely additive, nullable, no backfill. IDEMPOTENT — safe to re-run:
--   psql $DATABASE_URL -f migrations/052_add_device_relay_state.sql

BEGIN;

ALTER TABLE devices ADD COLUMN IF NOT EXISTS relay_cut BOOLEAN NULL;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS relay_state_at TIMESTAMP NULL;

COMMENT ON COLUMN devices.relay_cut IS 'Tracker-reported fuel/relay state (GT06 terminal-info bit 7): TRUE=cut, FALSE=connected, NULL=never reported.';
COMMENT ON COLUMN devices.relay_state_at IS 'UTC time the tracker last reported relay_cut.';

COMMIT;

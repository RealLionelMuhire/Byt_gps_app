-- Migration 055: overspeed_episodes
--
-- One row per continuous overspeed episode, as defined by
-- app/services/speed_limit.py's edge-triggered + hysteresis rule: it opens
-- on the fix that first crosses ABOVE Device.speed_limit_kmh (the same fix
-- that synthesizes the "Over speed" alarm) and closes on the first fix at
-- or under (threshold - hysteresis). Before this, an episode's end was
-- never recorded anywhere (only Device.is_overspeeding flipped back), and
-- the threshold that applied was lost as soon as the owner changed it.
--
-- The alarm itself stays a `locations` row (alarm_location_id links it);
-- the history endpoints (/route, /period-route) return these rows so the
-- red route segments and the alert are the same record and can't disagree.
--
--   end_reason: speed_dropped | signal_lost (closing fix came after an
--               offline gap; ended_at = last fix of the episode) |
--               limit_changed (owner changed/cleared the limit mid-episode)
--
-- threshold_kmh/hysteresis_kmh are snapshots at the moment the episode
-- opened. Purely additive. IDEMPOTENT — safe to re-run:
--   psql $DATABASE_URL -f migrations/055_add_overspeed_episodes.sql

BEGIN;

CREATE TABLE IF NOT EXISTS overspeed_episodes (
    id SERIAL PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    alarm_location_id INTEGER NULL REFERENCES locations(id) ON DELETE SET NULL,
    start_location_id INTEGER NULL REFERENCES locations(id) ON DELETE SET NULL,
    started_at TIMESTAMP NOT NULL,
    start_latitude DOUBLE PRECISION NOT NULL,
    start_longitude DOUBLE PRECISION NOT NULL,
    threshold_kmh DOUBLE PRECISION NOT NULL,
    hysteresis_kmh DOUBLE PRECISION NOT NULL,
    max_speed_kmh DOUBLE PRECISION NOT NULL,
    last_fix_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP NULL,
    end_latitude DOUBLE PRECISION NULL,
    end_longitude DOUBLE PRECISION NULL,
    end_reason VARCHAR(20) NULL
);

CREATE INDEX IF NOT EXISTS ix_overspeed_episodes_device_started ON overspeed_episodes (device_id, started_at);
CREATE INDEX IF NOT EXISTS ix_overspeed_episodes_alarm_location ON overspeed_episodes (alarm_location_id);
-- At most one open episode per device is the invariant speed_limit.py keeps.
CREATE INDEX IF NOT EXISTS ix_overspeed_episodes_open ON overspeed_episodes (device_id) WHERE ended_at IS NULL;

COMMENT ON TABLE overspeed_episodes IS 'Continuous overspeed episodes (see migration 055, app/services/speed_limit.py).';
-- Not exposed through Supabase's REST API (see migration 054).
ALTER TABLE overspeed_episodes ENABLE ROW LEVEL SECURITY;

COMMIT;

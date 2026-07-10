-- Migration 011: Add Expo Push Token
-- Adds expo_push_token to users so the backend can send push notifications
-- to device owners when a GPS alarm fires (via Expo Push API).

ALTER TABLE users ADD COLUMN IF NOT EXISTS expo_push_token VARCHAR(255) DEFAULT NULL;

COMMENT ON COLUMN users.expo_push_token IS 'Expo push notification token — stored by the mobile app on login, used by tcp_server.py to send alarm push notifications';

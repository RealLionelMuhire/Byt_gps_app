-- Migration 027: Add FCM push token
--
-- The mobile client is moving from Expo push tokens (users.expo_push_token,
-- see migration 011) to Firebase Cloud Messaging. Both columns coexist
-- during the transition — app/services/push_notifications.py prefers
-- fcm_token and falls back to expo_push_token — so existing app installs
-- keep working unchanged while updated clients register an FCM token
-- instead. PUT /api/auth/push-token auto-detects which kind of token it
-- received by shape and writes to the matching column.

ALTER TABLE users ADD COLUMN IF NOT EXISTS fcm_token VARCHAR(255) DEFAULT NULL;

COMMENT ON COLUMN users.fcm_token IS 'FCM registration token — stored by the mobile app on login/token refresh, preferred over expo_push_token by app/services/push_notifications.py';

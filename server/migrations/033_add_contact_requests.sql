-- Contact requests raised from the mobile app (e.g. "order a GPS device"
-- or "support") — visible to admins via GET /api/contact-requests.
CREATE TABLE IF NOT EXISTS contact_requests (
    id SERIAL PRIMARY KEY,
    clerk_user_id VARCHAR(255) NOT NULL,
    type VARCHAR(20) NOT NULL,
    name VARCHAR(255) NOT NULL,
    phone VARCHAR(32) NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT (now() AT TIME ZONE 'utc')
);
CREATE INDEX IF NOT EXISTS ix_contact_requests_clerk_user_id ON contact_requests (clerk_user_id);
CREATE INDEX IF NOT EXISTS ix_contact_requests_created_at ON contact_requests (created_at);

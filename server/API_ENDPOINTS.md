# Track IQ — API Reference

**Base URL:** `https://api.track-iq.tech`  
**Interactive docs (Swagger UI):** `https://api.track-iq.tech/docs`  
**Admin dashboard:** `https://api.track-iq.tech/admin/login`

---

## Authentication

All `/api/*` endpoints (except `/api/auth/sync` and `/api/auth/user/{id}`) require:

```
Authorization: Bearer <clerk_session_token>
```

The token is a short-lived JWT issued by Clerk after the user signs in on the mobile app. The server validates it against Clerk's JWKS endpoint.

**Admin dashboard** (`/admin/*`) uses a separate Clerk-based sign-in and a signed session cookie. Only users whose Clerk User ID is listed in `ADMIN_CLERK_USER_IDS` can access it.

---

## Status Codes Used

| Code | Meaning |
|---|---|
| 200 | Success |
| 201 | Created |
| 400 | Bad request / validation error |
| 401 | Missing or invalid Clerk token |
| 403 | Forbidden (e.g. wrong pairing PIN, not admin) |
| 404 | Resource not found |
| 409 | Conflict (e.g. IMEI already paired to another user) |
| 422 | Unprocessable entity (missing required field) |
| 500 | Server error |

---

## Health & Info

### `GET /`
Returns server name, version, and port info. No auth required.

**Response:**
```json
{
  "app": "GPS Tracking Server",
  "version": "1.0.0",
  "status": "running",
  "tcp_port": 7018,
  "http_port": 8000
}
```

### `GET /health`
Returns server health and live TCP connection counts. No auth required.

**Response:**
```json
{
  "status": "healthy",
  "tcp_connections": 3,
  "active_devices": 2
}
```

---

## Authentication Endpoints (`/api/auth`)

### `POST /api/auth/sync`
Syncs a Clerk-authenticated user into the local database (upsert). Called automatically by the mobile app on every sign-in. **No auth header required** — Clerk user ID is passed in the body.

**Request:**
```json
{
  "clerk_user_id": "user_2abc123xyz456def",
  "email": "user@example.com",
  "first_name": "Jane",
  "last_name": "Doe"
}
```

**Response `200`:**
```json
{
  "id": 42,
  "clerk_user_id": "user_2abc123xyz456def",
  "email": "user@example.com",
  "first_name": "Jane",
  "last_name": "Doe",
  "is_admin": false,
  "onboarding_step": 0,
  "onboarding_complete": false,
  "created_at": "2026-06-12T10:00:00Z",
  "updated_at": "2026-06-12T10:00:00Z"
}
```

---

### `GET /api/auth/me`
Returns the current authenticated user's profile.

**Headers:** `Authorization: Bearer <token>`

**Response `200`:** Same shape as sync response above.

**Response `401`:** Token invalid or user not synced yet.

---

### `GET /api/auth/user/{clerk_user_id}`
Retrieve a user by their Clerk User ID. No auth required.

**Response `200`:** Same shape as sync response.

**Response `404`:** User not found.

---

### `POST /api/auth/admin-create-user`
Create a new user account in Clerk and sync to the local DB. Used by admins to pre-provision accounts.

**Headers:** `X-Admin-Secret: <ADMIN_SECRET>`

**Request:**
```json
{
  "email": "driver@company.com",
  "password": "SecurePass123!",
  "first_name": "John",
  "last_name": "Doe"
}
```

**Response `201`:** Full user object.  
**Response `401`:** Invalid admin secret.

---

## Onboarding Endpoints (`/api`)

### `POST /api/users`
Create or update the user profile (identical to `/api/auth/sync` — used by the mobile onboarding flow).

**Headers:** `Authorization: Bearer <token>`

**Request:**
```json
{
  "clerk_user_id": "user_2abc123",
  "email": "user@example.com",
  "first_name": "Jane",
  "last_name": "Doe"
}
```

---

### `POST /api/devices/pair`
Link a whitelisted GPS device to the authenticated user's account. The device must already exist in the database (added by admin). If the device has a `pairing_pin`, it must be provided.

**Headers:** `Authorization: Bearer <token>`

**Request:**
```json
{
  "imei": "358765012345678",
  "pairingPin": "X7Y2B9"
}
```

**Response `200`:**
```json
{
  "success": true,
  "device_id": 7,
  "imei": "358765012345678",
  "status": "offline"
}
```

**Response `403`:** Incorrect or missing pairing PIN.  
**Response `404`:** IMEI not in inventory.  
**Response `409`:** Device already paired to another account.

---

### `GET /api/devices/{imei}/status`
Poll the connection status of a paired device. Used by the mobile app's "Waiting for Signal" screen.

**Headers:** `Authorization: Bearer <token>`

**Response `200`:**
```json
{
  "imei": "358765012345678",
  "status": "online",
  "last_connect": "2026-06-14T05:30:00Z",
  "last_latitude": -1.9403,
  "last_longitude": 29.8739
}
```

---

### `POST /api/vehicles`
Register a vehicle and link it to the authenticated user.

**Headers:** `Authorization: Bearer <token>`

**Request:**
```json
{
  "make": "Toyota",
  "model": "Hilux",
  "year": 2022,
  "plate": "RAC 123 A",
  "color": "White",
  "device_imei": "358765012345678"
}
```

**Response `201`:** Full vehicle object with `id`.

---

### `GET /api/vehicles`
List all vehicles belonging to the authenticated user.

**Headers:** `Authorization: Bearer <token>`

**Response `200`:** Array of vehicle objects.

---

### `POST /api/payments/verify`
Verify a Flutterwave payment and activate a subscription plan.

**Headers:** `Authorization: Bearer <token>`

**Request:**
```json
{
  "transaction_id": "12345678",
  "plan": "monthly"
}
```

**Response `200`:**
```json
{
  "success": true,
  "subscription": {
    "plan": "monthly",
    "expires_at": "2026-07-14T00:00:00Z"
  }
}
```

---

### `POST /api/subscriptions`
Create a new subscription for the current user (called after payment verification).

**Headers:** `Authorization: Bearer <token>`

**Request:**
```json
{
  "plan": "monthly",
  "transaction_id": "12345678"
}
```

---

### `POST /api/subscriptions/upgrade`
Upgrade an existing subscription to a higher tier.

**Headers:** `Authorization: Bearer <token>`

**Request:**
```json
{
  "new_plan": "yearly",
  "transaction_id": "12345679"
}
```

---

### `GET /api/billing`
Get the current user's billing status and active subscription details.

**Headers:** `Authorization: Bearer <token>`

**Response `200`:**
```json
{
  "has_active_subscription": true,
  "plan": "monthly",
  "expires_at": "2026-07-14T00:00:00Z",
  "days_remaining": 30
}
```

---

## Device Endpoints (`/api/devices`)

### `GET /api/devices/`
List all devices. Returns all devices visible to the authenticated user.

**Headers:** `Authorization: Bearer <token>`

**Query params:**
- `status` — filter by `online` or `offline`
- `skip` / `limit` — pagination

---

### `GET /api/devices/{device_id}`
Get a single device by numeric ID.

**Headers:** `Authorization: Bearer <token>`

---

### `GET /api/devices/imei/{imei}`
Get a device by its 15-digit IMEI number.

**Headers:** `Authorization: Bearer <token>`

---

### `POST /api/devices/`
Manually register a device (admin use only).

**Headers:** `Authorization: Bearer <token>`

**Request:**
```json
{
  "imei": "358765012345678",
  "name": "Fleet Tracker #42",
  "description": "Installed on Toyota Hilux RAC 123A"
}
```

---

### `GET /api/devices/{device_id}/trips`
List all trips recorded for a device.

**Headers:** `Authorization: Bearer <token>`

---

### `GET /api/devices/{device_id}/diagnostics`
Detailed diagnostics: signal intervals, stale status, last packet time.

**Headers:** `Authorization: Bearer <token>`

---

## Location Endpoints (`/api/locations`)

### `GET /api/locations/{device_id}/latest`
Get the most recent GPS location for a device.

### `GET /api/locations/{device_id}/history`
Get historical GPS points.

**Query params:**
- `start` / `end` — ISO 8601 datetime range
- `limit` — max results (default 100)

### `GET /api/locations/{device_id}/route`
Returns a GeoJSON LineString of the device's route for a given time window.

---

## Trip Endpoints (`/api/trips`)

### `GET /api/trips/{trip_id}`
Get a single trip by ID including start/end location and distance.

### `GET /api/trips/{trip_id}/route`
Get the full route GeoJSON for a trip.

---

## WebSocket

### `WS /ws/{device_id}`
Subscribe to real-time location updates for a device. The server pushes JSON location packets as they arrive from the GPS hardware via TCP.

**Message format:**
```json
{
  "type": "location",
  "device_id": 7,
  "latitude": -1.9403,
  "longitude": 29.8739,
  "speed": 45.5,
  "course": 270,
  "timestamp": "2026-06-14T05:35:00Z"
}
```

---

## Admin Dashboard (Web UI)

These routes serve the server-rendered HTML admin panel. They are protected by a Clerk-based session cookie — only users in `ADMIN_CLERK_USER_IDS` can access them.

| Route | Description |
|---|---|
| `GET /admin/login` | Clerk sign-in page |
| `POST /admin/auth/verify` | Exchange Clerk JWT for session cookie |
| `GET /admin/logout` | Clear session, redirect to login |
| `GET /admin/devices` | Device inventory management UI |
| `POST /admin/devices` | Add a new device to the whitelist |
| `POST /admin/devices/{imei}/delete` | Remove an unpaired device |
| `GET /dashboard` | Read-only fleet monitor (public) |

### `POST /admin/auth/verify`
Exchange a valid Clerk session JWT for a signed admin session cookie.

**Request:**
```json
{ "token": "<clerk_session_jwt>" }
```

**Response `200`:**
```json
{ "ok": true, "redirect": "/admin/devices" }
```

Sets `admin_session` cookie (HMAC-SHA256 signed, 8-hour TTL, HTTPS-only in production).

**Response `403`:** Valid token but user is not in `ADMIN_CLERK_USER_IDS`.

---

## GPS Hardware Protocol (TCP Port 7018)

The server listens on TCP port `7018` for binary packets from GPS trackers using the standard `0x7878` protocol.

### Login Packet (device → server)

Sent automatically when the tracker powers on and connects.

```
7878  11  01  IMEI(8 bytes)  SerialNum  CRC  0D 0A
```

**Server behaviour:**
- Looks up the IMEI in the `devices` table
- If **not found**: closes the TCP connection immediately (device not whitelisted)
- If **found**: marks device `online`, registers the live connection, sends ACK

### Location Packet (device → server)

```
7878  len  12  dateTime  lat  lon  speed  course  status  signalInfo  CRC  0D 0A
```

The server parses coordinates, saves to `locations` table, updates `devices.last_latitude/longitude`, and broadcasts to any connected WebSocket clients.

---

## Secure Device Registration Flow

```
Admin (Web UI)                  GPS Hardware               Mobile App (User)
     │                               │                           │
     │ POST /admin/devices           │                           │
     │ {imei, name, pairingPin}      │                           │
     │ ──────────────────────►DB     │                           │
     │                               │ TCP Login (IMEI)          │
     │                               │ ──────────────────────►   │
     │                               │  Server checks whitelist  │
     │                               │  ✅ Accept → mark online  │
     │                               │                           │
     │                               │                 POST /api/devices/pair
     │                               │                 {imei, pairingPin}
     │                               │                 ──────────────────►DB
     │                               │                 ✅ Link user_id
     │                               │                           │
     │                               │                 GET /api/devices/{imei}/status
     │                               │                 ← {status: "online"}
```

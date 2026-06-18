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
| 402 | Payment not found or not verified |
| 403 | Forbidden (e.g. wrong pairing PIN, not admin, plan limit) |
| 404 | Resource not found |
| 409 | Conflict (e.g. IMEI already paired, command failed, trial already used) |
| 422 | Unprocessable entity (missing required field) |
| 500 | Server error |
| 503 | TCP server not available |

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
Syncs a Clerk-authenticated user into the local database (upsert). Called automatically by the mobile app on every sign-in. **No auth header required.**

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

These endpoints power the mobile app's onboarding flow. All require `Authorization: Bearer <token>`.

### `POST /api/users`
Create or update the user profile (Step 4). Idempotent.

**Request:**
```json
{
  "firstName": "Jane",
  "lastName": "Doe",
  "email": "user@example.com",
  "role": "owner"
}
```

**Response `201`:**
```json
{
  "userId": 42,
  "alreadyExists": false
}
```

---

### `POST /api/devices/pair`
Link a whitelisted GPS device to the authenticated user's account (Step 5). The device must already exist in the database (added by admin). If the device has a `pairing_pin`, it must be provided.

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
  "deviceId": 7,
  "status": "offline",
  "imei": "358765012345678"
}
```

**Response `403`:** Incorrect or missing pairing PIN.  
**Response `404`:** IMEI not in inventory.  
**Response `409`:** Device already paired to another account.

---

### `GET /api/devices/{imei}/status`
Poll the connection status of a paired device by IMEI (Step 6). Used by the mobile app's "Waiting for Signal" screen.

**Response `200`:**
```json
{
  "status": "online"
}
```
Status values: `online`, `offline`, `pending`

---

### `POST /api/vehicles`
Register a vehicle and link it to the authenticated user (Step 7).

**Request:**
```json
{
  "nickname": "My Hilux",
  "plate": "RAC 123 A",
  "make": "Toyota",
  "model": "Hilux",
  "deviceImei": "358765012345678"
}
```

**Response `201`:**
```json
{
  "vehicleId": 3
}
```

**Response `403`:** Vehicle limit exceeded for current plan.

---

### `GET /api/vehicles`
List all vehicles belonging to the authenticated user, with live device status.

**Response `200`:**
```json
{
  "vehicles": [
    {
      "id": 3,
      "nickname": "My Hilux",
      "plate": "RAC 123 A",
      "make": "Toyota",
      "model": "Hilux",
      "device": {
        "id": 7,
        "imei": "358765012345678",
        "status": "online",
        "latitude": -1.9403,
        "longitude": 29.8739,
        "last_seen": "2026-06-18T20:00:00Z"
      },
      "created_at": "2026-06-12T10:00:00Z"
    }
  ]
}
```

---

### `POST /api/payments/verify`
Server-side Flutterwave payment verification (Step 8, paid plans). Never trust client-side success alone.

**Request:**
```json
{
  "txRef": "FLW-TX-123456",
  "planId": "basic"
}
```

`planId` values: `trial`, `basic`, `fleet`

**Response `200`:**
```json
{
  "verified": true,
  "status": "successful"
}
```

**Response `200` (failed verification):**
```json
{
  "verified": false,
  "error": "Payment verification failed"
}
```

---

### `POST /api/subscriptions`
Activate a subscription plan for the current user (Step 8). Call after `/api/payments/verify` for paid plans. Free trial does not require payment.

**Request:**
```json
{
  "planId": "trial"
}
```

**Response `201`:**
```json
{
  "subscriptionId": 5,
  "expiresAt": "2026-07-14T00:00:00Z"
}
```

**Response `409`:** Free trial already used.

---

### `POST /api/subscriptions/upgrade`
Upgrade an existing subscription to a higher tier. Call after `/api/payments/verify`.

**Request:**
```json
{
  "planId": "fleet",
  "txRef": "FLW-TX-123457"
}
```

**Response `201`:** Same as create subscription.  
**Response `400`:** Cannot downgrade.  
**Response `402`:** Payment not verified.

---

### `GET /api/billing`
Get the current user's active plan and payment history.

**Response `200`:**
```json
{
  "currentPlan": "basic",
  "expiresAt": "2026-07-14T00:00:00Z",
  "payments": [
    {
      "txRef": "FLW-TX-123456",
      "planId": "basic",
      "amount": 5000,
      "status": "successful",
      "createdAt": "2026-06-14T10:00:00Z"
    }
## Device Lifecycle (Inventory Management)

Every device has a `lifecycle` field that tracks its ownership state:

| Lifecycle | `user_id` | Meaning |
|---|---|---|
| `registered` | `null` | Admin added the IMEI to DB and inserted the SIM card. Device has **never connected via TCP**. Not ready to sell. |
| `in_stock` | `null` | Device sent its first TCP handshake — proven operational. **Ready to sell.** Still owned by the company. |
| `sold` | set | Device is paired to a customer account. Customer has full ownership. |

### Transition Rules

```
Admin registers IMEI
       ↓
  [registered]   ← user_id=null, never connected
       ↓
  Device powers on, SIM connects, TCP handshake received (0x01 login packet)
       ↓ (automatic, no action needed)
  [in_stock]     ← user_id=null, proven functional, ready to sell
       ↓
  Optional: admin sends STATUS#/PARAM# command to confirm, then calls /verify
       ↓
  Customer scans/enters IMEI + pairing PIN in mobile app → POST /api/devices/pair
       ↓ (lifecycle set to 'sold' automatically)
  [sold]         ← user_id=customer_id, customer owns device
```

> **Key rule:** A customer **cannot pair a `registered` device**. The device must have connected at least once (i.e. be `in_stock`) before it can be sold to a customer. This prevents selling non-functional devices.

---

## Device Endpoints (`/api/devices`)

All require `Authorization: Bearer <token>`.

### `GET /api/devices/`
List all devices visible to the authenticated user.

**Query params:**
- `status` — filter by `online` or `offline`
- `lifecycle` filter is available via the admin dashboard
- `skip` / `limit` — pagination (default limit: 100, max: 1000)

---

### `GET /api/devices/{device_id}`
Get a single device by numeric ID.

---

### `GET /api/devices/imei/{imei}`
Get a device by its 15-digit IMEI number.

---

### `POST /api/devices/`
Whitelist a new GPS device in the inventory. If `pairing_pin` is omitted, a secure 6-character PIN is auto-generated. Device starts with `lifecycle: "registered"`.

**Request:**
```json
{
  "imei": "358765012345678",
  "name": "Fleet Tracker #42",
  "description": "Installed on Toyota Hilux RAC 123A",
  "sim_number": "+250781234567",
  "pairing_pin": "ABC123"
}
```

**Response `201`:** Full device object including generated `pairing_pin` and `lifecycle: "registered"`.

---

### `PUT /api/devices/{device_id}`
Update device name, description, or SIM number.

**Request:**
```json
{
  "name": "New Name",
  "description": "Updated notes",
  "sim_number": "+250789999999"
}
```

---

### `DELETE /api/devices/{device_id}`
Delete device and all associated location data. Status 204 on success.

---

### `POST /api/devices/{device_id}/assign`
Assign a device to the first user in the database (legacy/admin utility). Sets `lifecycle: "sold"`. No auth required.

**Response `200`:**
```json
{
  "message": "Device assigned successfully",
  "device_id": 7,
  "user_id": 1,
  "lifecycle": "sold"
}
```

---

### `POST /api/devices/{device_id}/verify`
**Admin:** Manually mark a device as verified and ready to sell (`in_stock`). Optional — the TCP handshake already auto-promotes. Use this to force-promote or to confirm after sending a STATUS# command.

**Response `200`:**
```json
{
  "device_id": 7,
  "imei": "358765012345678",
  "previous_lifecycle": "registered",
  "lifecycle": "in_stock",
  "message": "Device marked as verified and ready to sell."
}
```

**Response `409`:** Device is already `sold`.

---

### `GET /api/devices/{device_id}/status`
Get current device status including battery, signal, and last known location.

**Response `200`:**
```json
{
  "id": 7,
  "imei": "358765012345678",
  "name": "Fleet Tracker #42",
  "status": "online",
  "last_update": "2026-06-18T20:00:00Z",
  "battery_level": 80,
  "gsm_signal": 22,
  "location": {
    "latitude": -1.9403,
    "longitude": 29.8739
  }
}
```

---

### `GET /api/devices/{device_id}/trips`
List all trips recorded for a specific device.

---

### `GET /api/devices/{device_id}/diagnostics`
Detailed diagnostics: packet interval stats, stale status, last packet time.

**Query params:**
- `samples` — number of recent location points to analyze (default: 20, max: 200)

**Response `200`:**
```json
{
  "device_id": 7,
  "imei": "358765012345678",
  "status": "online",
  "last_connect": "2026-06-18T19:55:00Z",
  "last_update": "2026-06-18T20:00:00Z",
  "last_location_timestamp": "2026-06-18T20:00:00Z",
  "seconds_since_last_update": 35,
  "sending_status": "Sending",
  "location_intervals": {
    "samples": 19,
    "avg_seconds": 21.3,
    "min_seconds": 18.0,
    "max_seconds": 25.1,
    "last_interval_seconds": 20.5
  }
}
```

`sending_status` values: `Sending`, `Stale`, `Offline (timed out)`, `No data`

---

## Device Command Endpoints (`/api/devices`)

All require `Authorization: Bearer <token>`. Commands are sent over the existing TCP connection (Protocol 0x80) — no SMS needed.

### `POST /api/devices/{device_id}/command`
Send any raw SMS-compatible command to a connected device.

**Request:**
```json
{
  "command": "STATUS#"
}
```

**Response `200`:**
```json
{
  "device_id": 7,
  "imei": "358765012345678",
  "command_sent": "STATUS#",
  "device_response": "V:4.2V;CSQ:22;GPS:1;ACC:1",
  "note": null
}
```

**Response `409`:** Device not connected via TCP.

---

### `POST /api/devices/{device_id}/alarm/vibration`
Enable or disable vibration/shock alarm.

**Request:** `{ "enabled": true }`

---

### `POST /api/devices/{device_id}/alarm/lowbattery`
Enable or disable low battery alarm.

**Request:** `{ "enabled": true }`

---

### `POST /api/devices/{device_id}/alarm/acc`
Enable or disable ACC (ignition on/off) alarm.

**Request:** `{ "enabled": true }`

---

### `POST /api/devices/{device_id}/alarm/overspeed`
Enable or disable overspeed alarm with configurable threshold.

**Request:** `{ "enabled": true, "speed_kmh": 100 }`

---

### `POST /api/devices/{device_id}/alarm/displacement`
Enable or disable displacement/movement alarm with configurable radius.

**Request:** `{ "enabled": true, "radius_meters": 200 }`

---

### `POST /api/devices/{device_id}/alarm/sos`
Enable or disable SOS alarm.

**Request:** `{ "enabled": true }`

---

### `POST /api/devices/{device_id}/fuel/cut`
Cut oil/electricity to immobilize the vehicle. Only safe when speed < 20 km/h.

No request body required.

---

### `POST /api/devices/{device_id}/fuel/restore`
Restore oil/electricity (re-enable vehicle).

No request body required.

---

### `POST /api/devices/{device_id}/query/location`
Request current location from device (device replies via TCP).

---

### `POST /api/devices/{device_id}/query/status`
Request current device status from device.

---

## Location Endpoints (`/api/locations`)

All require `Authorization: Bearer <token>`.

### `GET /api/locations/{device_id}/latest`
Get the most recent GPS location for a device.

**Response `200`:**
```json
{
  "id": 1001,
  "device_id": 7,
  "latitude": -1.9403,
  "longitude": 29.8739,
  "speed": 45.5,
  "course": 270,
  "satellites": 11,
  "gps_valid": true,
  "is_alarm": false,
  "alarm_type": null,
  "timestamp": "2026-06-18T20:00:00Z",
  "received_at": "2026-06-18T20:00:01Z"
}
```

---

### `GET /api/locations/{device_id}/history`
Get historical GPS points. Defaults to last 24 hours.

**Query params:**
- `start_time` / `end_time` — ISO 8601 UTC datetime
- `limit` — max results (default: 1000, max: 10000)

**Response `200`:**
```json
{
  "device_id": 7,
  "device_name": "Fleet Tracker #42",
  "device_imei": "358765012345678",
  "total_points": 4320,
  "locations": [ ... ]
}
```

---

### `GET /api/locations/{device_id}/route`
Returns a GeoJSON FeatureCollection of the device's route. Defaults to last 24 hours.

**Query params:**
- `start_time` / `end_time` — ISO 8601 UTC datetime
- `simplify` — boolean, reduce point count (default: false)

**Response `200`:** GeoJSON FeatureCollection with Point features, each carrying `timestamp`, `speed`, `course`, `is_alarm`.

---

### `GET /api/locations/{device_id}/route-line`
Returns route as a GeoJSON LineString with timestamps aligned to coordinates. Useful for trip playback.

**Query params:** `start_time`, `end_time`

**Response `200`:**
```json
{
  "type": "LineString",
  "coordinates": [[-1.9403, 29.8739], ...],
  "timestamps": ["2026-06-18T20:00:00Z", ...],
  "speeds": [45.5, ...],
  "courses": [270, ...],
  "properties": {
    "device_id": 7,
    "device_name": "Fleet Tracker #42",
    "device_imei": "358765012345678",
    "start_time": "...",
    "end_time": "...",
    "point_count": 240
  }
}
```

---

### `GET /api/locations/{device_id}/distance`
Get total distance covered in a time range (Haversine, GPS-valid points only). Defaults to last 24 hours.

**Query params:** `start_time`, `end_time`

**Response `200`:**
```json
{
  "device_id": 7,
  "device_name": "Fleet Tracker #42",
  "device_imei": "358765012345678",
  "start_time": "...",
  "end_time": "...",
  "point_count": 240,
  "total_distance_km": 34.7
}
```

---

### `GET /api/locations/{device_id}/alarms`
Get alarm events for a device. Defaults to last 7 days.

**Query params:** `start_time`, `end_time`, `limit` (max: 1000)

**Response `200`:** Array of location objects where `is_alarm: true`.

---

### `GET /api/locations/nearby`
Find devices near a GPS coordinate. No auth required.

**Query params:**
- `latitude` (required)
- `longitude` (required)
- `radius_km` — search radius in km (default: 10, max: 100)

**Response `200`:**
```json
{
  "center": { "latitude": -1.94, "longitude": 29.87 },
  "radius_km": 10,
  "devices_found": 2,
  "devices": [
    {
      "device_id": 7,
      "device_name": "Fleet Tracker #42",
      "imei": "358765012345678",
      "latitude": -1.9403,
      "longitude": 29.8739,
      "distance_km": 0.4,
      "last_update": "2026-06-18T20:00:00Z"
    }
  ]
}
```

---

## Trip Endpoints (`/api/trips`)

All require `Authorization: Bearer <token>` unless noted otherwise.

### `GET /api/trips`
List all saved trips for a device.

**Query params:**
- `device_id` (required)

**Response `200`:** Array of trip objects.

---

### `POST /api/trips`
Create a saved trip from a device's location history. Reverse-geocodes start/end automatically.

**Request:**
```json
{
  "device_id": 7,
  "name": "Morning commute",
  "start_time": "2026-06-18T06:00:00Z",
  "end_time": "2026-06-18T07:30:00Z"
}
```

**Response `201`:**
```json
{
  "id": 12,
  "device_id": 7,
  "user_id": 42,
  "name": "Morning commute",
  "display_name": "Kigali Heights → Kacyiru",
  "start_time": "2026-06-18T06:00:00Z",
  "end_time": "2026-06-18T07:30:00Z",
  "total_distance_km": 8.3,
  "created_at": "2026-06-18T07:31:00Z"
}
```

---

### `POST /api/trips/start`
Start an active (open-ended) trip. Trip ends automatically when device stops sending.

**Request:**
```json
{
  "device_id": 7,
  "name": "Delivery run"
}
```

**Response `201`:** Trip object with `end_time: null`.

**Response `400`:** Device already has an active trip.

---

### `GET /api/trips/suggested`
Suggest trip segments from location history based on stop duration settings.

**Query params:**
- `device_id` (required)
- `start_time`, `end_time` — defaults to last 24 hours

**Response `200`:** Array of suggested trip segments with distance, duration, and coordinates.

---

### `GET /api/trips/settings`
Get trip segmentation settings for the current user.

**Response `200`:**
```json
{
  "stop_splits_trip_after_minutes": 60,
  "minimum_trip_duration_minutes": 5,
  "stop_speed_threshold_kmh": 5.0
}
```

---

### `PUT /api/trips/settings`
Update trip segmentation settings.

**Request:** Any subset of the settings fields.

```json
{
  "stop_splits_trip_after_minutes": 30,
  "stop_speed_threshold_kmh": 3.0
}
```

---

### `GET /api/trips/{trip_id}`
Get trip metadata and full route geometry.

**Query params:**
- `device_id` (required)

**Response `200`:** Trip object with additional `device_name`, `device_imei`, and `route` (LineString).

---

### `POST /api/trips/{trip_id}/end`
Manually end an active trip before device disconnects.

**Query params:**
- `device_id` (required)

**Response `200`:** Updated trip object with `end_time` set.

---

### `DELETE /api/trips/{trip_id}`
Delete a saved trip. Location data is NOT affected.

**Query params:**
- `device_id` (required)

**Response `204`:** No content.

---

## WebSocket

### `WS /ws/locations/{device_id}`
Subscribe to real-time location updates for a device. The server pushes JSON location packets as they arrive from the GPS hardware via TCP.

**Query params:**
- `token` — Clerk session JWT (required in production)

**Connection example:**
```
wss://api.track-iq.tech/ws/locations/7?token=<clerk_jwt>
```

**Location message:**
```json
{
  "type": "location",
  "device_id": 7,
  "latitude": -1.9403,
  "longitude": 29.8739,
  "speed": 45.5,
  "course": 270,
  "timestamp": "2026-06-18T20:00:00Z",
  "gps_valid": true
}
```

**Alarm message:**
```json
{
  "type": "alarm",
  "device_id": 7,
  "alarm_type": "SOS",
  "latitude": -1.9403,
  "longitude": 29.8739,
  "timestamp": "2026-06-18T20:00:00Z"
}
```

Client may send any text frame as a keep-alive ping (ignored by server). Connection times out after 60s of inactivity.

---

## Admin Dashboard (Web UI)

Protected by Clerk-based session cookie. Only users in `ADMIN_CLERK_USER_IDS` can access.

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

The server listens on TCP port `7018` for binary packets from GPS trackers using the `0x7878` GT06 protocol.

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
7878  len  12  dateTime  gpsInfo  lat  lon  speed  courseStatus  LBS  Serial  CRC  0D 0A
```

The server parses coordinates, saves to `locations` table, updates `devices.last_latitude/longitude`, and broadcasts to any connected WebSocket clients.

### Heartbeat Packet (device → server)

```
7878  0A  13  terminalInfo  voltage  gsmSignal  alarmLang  Serial  CRC  0D 0A
```

Updates `devices.battery_level` and `devices.gsm_signal`.

### Supported Packet Types

| Protocol | Type | Direction |
|---|---|---|
| `0x01` | Login (IMEI auth) | Device → Server |
| `0x12` | Location (GPS coordinates) | Device → Server |
| `0x13` | Heartbeat (battery, signal) | Device → Server |
| `0x15` | Command response | Device → Server |
| `0x16` | Alarm event | Device → Server |
| `0x80` | Command | Server → Device |

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

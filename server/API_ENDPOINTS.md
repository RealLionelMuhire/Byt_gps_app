# API Endpoints Documentation
## BYThron GPS Tracking Server

**Base URL:** `https://byatron.tech`
**API Version:** 1.0.0
**Last Updated:** May 2026

> **Breaking change (May 2026):** All device, location, command, and trip endpoints now require a valid Clerk session token in the `Authorization` header. See [Authentication](#authentication) below.

---

## Table of Contents

1. [Authentication](#authentication)
2. [Core / Health Endpoints](#core--health-endpoints)
3. [Authentication Endpoints](#authentication-endpoints)
4. [Device Management Endpoints](#device-management-endpoints)
5. [Location & Tracking Endpoints](#location--tracking-endpoints)
6. [Device Commands](#device-commands)
7. [Trip Endpoints](#trip-endpoints)
8. [Onboarding & Subscription Endpoints](#onboarding--subscription-endpoints)
9. [WebSocket — Real-time Location Stream](#websocket--real-time-location-stream)
10. [Dashboard (Web UI)](#dashboard-web-ui)
11. [Error Reference](#error-reference)
12. [Developer Notes](#developer-notes)

---

## Authentication

### How to Authenticate

All **device**, **location**, **command**, and **trip** endpoints require a Clerk session JWT in the request header:

```
Authorization: Bearer <clerk-session-token>
```

#### Getting the token

**Mobile (React Native + Clerk):**
```typescript
const { getToken } = useAuth();
const token = await getToken();
// then attach:
headers: { "Authorization": `Bearer ${token}` }
```

**Web (Clerk JS):**
```javascript
const token = await Clerk.session.getToken();
```

#### What happens without a token

```
HTTP 401 Unauthorized
{ "detail": "Missing Authorization header. Expected: Bearer <clerk-token>" }
```

### Open Endpoints (no token required)

| Endpoint | Reason |
|---|---|
| `GET /` | Health check |
| `GET /health` | Health check |
| `GET /dashboard` | Internal web UI |
| `POST /api/auth/sync` | Bootstrap — called before a token is stored |
| `POST /api/auth/admin-create-user` | Protected by `X-Admin-Secret` header instead |

### Dev / Local Mode

If `CLERK_SECRET_KEY` is not set in the server's `.env`, token verification is **skipped** and all requests are accepted. This is intentional to keep local development simple.

---

## Core / Health Endpoints

### `GET /`

Returns basic server information. No auth required.

**Response (200):**
```json
{
  "app": "GPS Tracking Server",
  "version": "1.0.0",
  "status": "running",
  "tcp_port": 7018,
  "http_port": 8000
}
```

---

### `GET /health`

Server health and active connection counts. No auth required.

**Response (200):**
```json
{
  "status": "healthy",
  "tcp_connections": 5,
  "active_devices": 3
}
```

---

## Authentication Endpoints

Prefix: `/api/auth`

### `POST /api/auth/sync` *(open)*

Creates or updates a user record from Clerk sign-in data. Called automatically by the mobile app after every sign-in.

> **No `Authorization` header required.** This is the bootstrap call — a token has not yet been obtained when this is first called.

**Request Body:**
```json
{
  "clerk_user_id": "user_2abc123xyz456def",
  "email": "user@example.com",
  "name": "John Doe"
}
```

**Response (200):**
```json
{
  "id": 1,
  "clerk_user_id": "user_2abc123xyz456def",
  "email": "user@example.com",
  "name": "John Doe",
  "is_admin": false,
  "created_at": "2026-02-08T10:00:00Z",
  "updated_at": "2026-02-08T10:00:00Z"
}
```

> The **first user ever synced** to the database automatically receives `is_admin: true`.

---

### `GET /api/auth/user/{clerk_user_id}` *(open)*

Retrieves a user record by Clerk user ID.

**Path Parameters:**
- `clerk_user_id` (string)

**Response (200):** Same shape as sync response above.

**Errors:** `404` User not found

---

### `POST /api/auth/admin-create-user` *(admin only)*

Creates a new user in Clerk and syncs them to the local database.

**Required Header:**
```
X-Admin-Secret: <your-admin-secret>
```

**Request Body:**
```json
{
  "email": "newuser@example.com",
  "password": "StrongPassword123!",
  "first_name": "Jane",
  "last_name": "Doe"
}
```

**Response (201):** Returns created user object.

**Errors:**
- `401` Invalid admin secret
- `500` `CLERK_SECRET_KEY` not configured on server

---

### `GET /api/auth/me` *(protected)*

🔒 **Requires `Authorization: Bearer <token>`**

Retrieves the current authenticated user's profile.

**Response (200):**
```json
{
  "id": 1,
  "clerk_user_id": "user_2abc123xyz456def",
  "email": "user@example.com",
  "first_name": "John",
  "last_name": "Doe",
  "is_admin": false,
  "onboarding_step": 4,
  "onboarding_complete": false,
  "created_at": "2026-02-08T10:00:00Z",
  "updated_at": "2026-02-08T10:00:00Z"
}
```

**Errors:**
- `401` Unauthorized
- `404` User not found

---

## Device Management Endpoints

Prefix: `/api/devices`
🔒 **All endpoints require `Authorization: Bearer <token>`**

---

### `GET /api/devices/`

Lists all GPS tracker devices.

**Query Parameters:**
- `skip` (int, default: 0): Pagination offset
- `limit` (int, default: 100, max: 1000): Page size
- `status` (string, optional): Filter — `online` or `offline`

**Response (200):**
```json
[
  {
    "id": 1,
    "imei": "123456789012345",
    "name": "Company Vehicle 1",
    "description": "Main delivery truck",
    "status": "online",
    "user_id": 1,
    "last_connect": "2026-05-20T10:30:00Z",
    "last_update": "2026-05-20T10:35:00Z",
    "last_latitude": -1.9403,
    "last_longitude": 29.8739,
    "battery_level": 85,
    "gsm_signal": 25,
    "created_at": "2026-01-01T00:00:00Z"
  }
]
```

---

### `GET /api/devices/{device_id}`

Get a single device by ID.

**Path Parameters:**
- `device_id` (int)

**Response (200):** Single device object (same shape as above)

**Errors:** `404`

---

### `GET /api/devices/imei/{imei}`

Get a device by IMEI number.

**Path Parameters:**
- `imei` (string): 15-digit IMEI

**Response (200):** Single device object

**Errors:** `404`

---

### `POST /api/devices/`

Manually register a new GPS tracker device.

**Request Body:**
```json
{
  "imei": "123456789012345",
  "name": "Truck 01",
  "description": "Main delivery truck"
}
```

**Response (201):** Created device object

**Errors:** `400` IMEI already exists

---

### `PUT /api/devices/{device_id}`

Update device name or description.

**Path Parameters:**
- `device_id` (int)

**Request Body:**
```json
{
  "name": "Updated Name",
  "description": "Updated description"
}
```

**Response (200):** Updated device object

**Errors:** `404`

---

### `DELETE /api/devices/{device_id}`

Permanently remove a device.

**Path Parameters:**
- `device_id` (int)

**Response (204):** No content

**Errors:** `404`

> ⚠️ This may cascade-delete location history depending on database constraints.

---

### `POST /api/devices/{device_id}/assign`

Assign a device to the first available user in the database.

**Response (200):**
```json
{
  "message": "Device assigned successfully",
  "device_id": 1,
  "user_id": 1
}
```

**Errors:** `404` Device or user not found

---

### `GET /api/devices/{device_id}/status`

Quick status summary — battery, signal, last position.

**Response (200):**
```json
{
  "id": 1,
  "imei": "123456789012345",
  "name": "Company Vehicle 1",
  "status": "online",
  "last_update": "2026-05-20T10:35:00Z",
  "battery_level": 85,
  "gsm_signal": 25,
  "location": {
    "latitude": -1.9403,
    "longitude": 29.8739
  }
}
```

---

### `GET /api/devices/{device_id}/diagnostics`

Detailed diagnostics including recent location packet timing statistics.

**Query Parameters:**
- `samples` (int, default: 20, max: 200): Number of recent location points to analyze

**Response (200):**
```json
{
  "device_id": 1,
  "imei": "123456789012345",
  "status": "online",
  "last_connect": "2026-05-20T10:30:00Z",
  "last_update": "2026-05-20T10:35:00Z",
  "last_location_timestamp": "2026-05-20T10:34:58Z",
  "seconds_since_last_update": 12,
  "sending_status": "Sending",
  "location_intervals": {
    "samples": 19,
    "avg_seconds": 30.2,
    "min_seconds": 28.0,
    "max_seconds": 35.5,
    "last_interval_seconds": 29.8
  }
}
```

`sending_status` values:
- `"Sending"` — packet received within `DEVICE_SENDING_STALE_SECONDS` (default 120s)
- `"Stale"` — no packet for 120–300s
- `"Offline (timed out)"` — no packet for over 300s
- `"No data"` — device has never sent a packet

---

### `GET /api/devices/{device_id}/trips`

List all trips for a device. See [Trip Endpoints](#trip-endpoints) for the full trip object shape.

---

## Location & Tracking Endpoints

Prefix: `/api/locations`
🔒 **All endpoints require `Authorization: Bearer <token>`**

> **Note:** For live tracking, use the [WebSocket endpoint](#websocket--real-time-location-stream) instead of polling this API.

---

### `GET /api/locations/{device_id}/latest`

Most recent location point for a device.

**Response (200):**
```json
{
  "id": 12345,
  "device_id": 1,
  "latitude": -1.9403,
  "longitude": 29.8739,
  "speed": 45.5,
  "course": 180,
  "satellites": 12,
  "gps_valid": true,
  "is_alarm": false,
  "alarm_type": null,
  "timestamp": "2026-05-20T10:35:00Z",
  "received_at": "2026-05-20T10:35:02Z"
}
```

**Errors:** `404` No location data found

---

### `GET /api/locations/{device_id}/history`

Historical location points for route playback and analysis.

**Query Parameters:**
- `start_time` (datetime, optional, default: 24h ago): ISO 8601 UTC
- `end_time` (datetime, optional, default: now): ISO 8601 UTC
- `limit` (int, default: 1000, max: 10000)

**Response (200):**
```json
{
  "device_id": 1,
  "device_name": "Company Vehicle 1",
  "device_imei": "123456789012345",
  "total_points": 1500,
  "locations": [ /* array of location objects */ ]
}
```

---

### `GET /api/locations/{device_id}/route`

Route as a **GeoJSON FeatureCollection** — ready for Mapbox, Leaflet, or Google Maps.

**Query Parameters:**
- `start_time` (datetime, optional, default: 24h ago)
- `end_time` (datetime, optional, default: now)
- `simplify` (bool, default: false): Reduce point count for large routes

**Response (200):** GeoJSON FeatureCollection where each Feature is a GPS point.

---

### `GET /api/locations/{device_id}/route-line`

Route as a **GeoJSON LineString** with aligned timestamp/speed/course arrays. Useful for animated route playback.

**Query Parameters:**
- `start_time` (datetime, optional, default: 24h ago)
- `end_time` (datetime, optional, default: now)

**Response (200):**
```json
{
  "type": "LineString",
  "coordinates": [[-74.006, 40.7128], [-74.005, 40.7130]],
  "timestamps": ["2026-05-20T10:00:00Z", "2026-05-20T10:00:30Z"],
  "speeds": [45.5, 47.0],
  "courses": [180, 182],
  "properties": {
    "device_id": 1,
    "device_name": "Company Vehicle 1",
    "device_imei": "123456789012345",
    "start_time": "2026-05-20T09:00:00Z",
    "end_time": "2026-05-20T10:35:00Z",
    "point_count": 150
  }
}
```

---

### `GET /api/locations/{device_id}/distance`

Total distance covered by a device in a given time range (Haversine formula, GPS-valid points only).

**Query Parameters:**
- `start_time` (datetime, optional, default: 24h ago)
- `end_time` (datetime, optional, default: now)

**Response (200):**
```json
{
  "device_id": 1,
  "device_name": "Company Vehicle 1",
  "device_imei": "123456789012345",
  "start_time": "2026-05-20T09:00:00Z",
  "end_time": "2026-05-20T10:35:00Z",
  "point_count": 150,
  "total_distance_km": 48.32
}
```

---

### `GET /api/locations/{device_id}/alarms`

Alarm events (SOS, shock, low battery, overspeed, etc.) for a device.

**Query Parameters:**
- `start_time` (datetime, optional, default: 7 days ago)
- `end_time` (datetime, optional, default: now)
- `limit` (int, default: 100, max: 1000)

**Response (200):** Array of location objects where `is_alarm: true` and `alarm_type` is set.

---

### `GET /api/locations/nearby` *(no auth required)*

Find devices within a radius of a coordinate. Useful for dispatch or proximity search.

**Query Parameters:**
- `latitude` (float, required): Center latitude (-90 to 90)
- `longitude` (float, required): Center longitude (-180 to 180)
- `radius_km` (float, default: 10, max: 100)

**Response (200):**
```json
{
  "center": { "latitude": -1.94, "longitude": 29.87 },
  "radius_km": 10,
  "devices_found": 3,
  "devices": [
    {
      "device_id": 1,
      "device_name": "Truck 01",
      "imei": "123456789012345",
      "latitude": -1.942,
      "longitude": 29.875,
      "distance_km": 0.25,
      "last_update": "2026-05-20T10:35:00Z"
    }
  ]
}
```

---

## Device Commands

Prefix: `/api/devices`
🔒 **All endpoints require `Authorization: Bearer <token>`**

Commands are sent to the GPS device over the existing TCP connection using Protocol 0x80.
No SMS balance is needed. The device replies via Protocol 0x15.

> ⚠️ Fuel cut commands only work when speed < 20 km/h and GPS is active.

---

### `POST /api/devices/{device_id}/command`

Send any raw SMS-compatible command string.

**Request Body:**
```json
{ "command": "STATUS#" }
```

**Response (200):**
```json
{
  "device_id": 1,
  "imei": "123456789012345",
  "command_sent": "STATUS#",
  "device_response": "Lat:...",
  "note": null
}
```

If the device doesn't reply within 10s, `device_response` will be `null` and `note` will explain the command was sent but not acknowledged.

**Errors:** `409` Device not connected via TCP

---

### Convenience Command Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/{device_id}/alarm/vibration` | Toggle shock/vibration alarm |
| `POST` | `/{device_id}/alarm/lowbattery` | Toggle low battery alarm |
| `POST` | `/{device_id}/alarm/acc` | Toggle ACC (ignition) alarm |
| `POST` | `/{device_id}/alarm/overspeed` | Toggle overspeed alarm |
| `POST` | `/{device_id}/alarm/displacement` | Toggle movement/displacement alarm |
| `POST` | `/{device_id}/alarm/sos` | Configure SOS alarm mode |
| `POST` | `/{device_id}/fuel/cut` | Cut oil/electricity (immobilize) |
| `POST` | `/{device_id}/fuel/restore` | Restore oil/electricity |
| `POST` | `/{device_id}/query/location` | Request current location from device |
| `POST` | `/{device_id}/query/status` | Request battery/GPS/GSM/ACC status |

**Request body for toggle endpoints:**
```json
{ "enabled": true }
```

**Request body for overspeed:**
```json
{ "enabled": true, "speed_kmh": 100 }
```

**Request body for displacement alarm:**
```json
{ "enabled": true, "radius_meters": 200 }
```

---

## Trip Endpoints

Prefix: `/api/trips`
🔒 **All endpoints require `Authorization: Bearer <token>`**

Trips are automatically created when a device starts moving and ended when it stops (or disconnects).

---

### `GET /api/trips/`

List all trips, newest first.

**Query Parameters:**
- `device_id` (int, optional): Filter by device
- `skip` (int, default: 0)
- `limit` (int, default: 50, max: 200)

**Response (200):** Array of trip objects.

---

### `GET /api/trips/{trip_id}`

Get a single trip by ID.

---

### `GET /api/trips/{trip_id}/route`

Full route (LineString with timestamps) for a specific trip. Same shape as `/api/locations/{device_id}/route-line`.

---

### Trip Object Shape

```json
{
  "id": 42,
  "device_id": 1,
  "start_time": "2026-05-20T08:00:00Z",
  "end_time": "2026-05-20T09:15:00Z",
  "start_latitude": -1.9403,
  "start_longitude": 29.8739,
  "end_latitude": -1.9870,
  "end_longitude": 30.1040,
  "distance_km": 28.4,
  "duration_seconds": 4500,
  "created_at": "2026-05-20T09:15:01Z"
}
```

---

## Onboarding & Subscription Endpoints

🔒 **All endpoints require `Authorization: Bearer <token>`**

Endpoints supporting user onboarding steps (profile sync, device pairing, vehicle association, plan/subscription activations).

---

### `POST /api/users`

Step 4 of onboarding: Upsert user profile after Clerk verification. This endpoint is idempotent.

**Request Body:**
```json
{
  "firstName": "John",
  "lastName": "Doe",
  "email": "john.doe@example.com",
  "role": "owner"
}
```

**Response (201):**
```json
{
  "userId": 1,
  "alreadyExists": false
}
```

---

### `POST /api/devices/pair`

Step 5 of onboarding: Link a pre-registered GPS tracking device (by IMEI) to the current user's account.

**Request Body:**
```json
{
  "imei": "123456789012345"
}
```

**Response (200):**
```json
{
  "deviceId": 1,
  "status": "pending",
  "imei": "123456789012345"
}
```

**Errors:**
- `400` Invalid IMEI format (must be 15 digits)
- `404` Device not found (must be pre-loaded in DB by admin)
- `409` Device already registered to another account

---

### `GET /api/devices/{imei}/status`

Step 6 of onboarding: Polls the connection status of a paired device by its IMEI. Transits from `pending` to `online` once the first TCP packet arrives from the hardware.

**Path Parameters:**
- `imei` (string): 15-digit IMEI number

**Response (200):**
```json
{
  "status": "online"
}
```

---

### `POST /api/vehicles`

Step 7 of onboarding: Register a vehicle and link it to the paired GPS device IMEI.

**Request Body:**
```json
{
  "nickname": "Office Car",
  "plate": "RAC 123 A",
  "make": "Toyota",
  "model": "Hilux",
  "deviceImei": "123456789012345"
}
```

**Response (201):**
```json
{
  "vehicleId": 1
}
```

**Errors:**
- `403` Device not paired to your account, or plan limit reached (e.g. Trial plan allows 1 vehicle max)

---

### `GET /api/vehicles`

Lists all registered vehicles for the authenticated user, including current device location and status.

**Response (200):**
```json
{
  "vehicles": [
    {
      "id": 1,
      "nickname": "Office Car",
      "plate": "RAC 123 A",
      "make": "Toyota",
      "model": "Hilux",
      "device": {
        "id": 1,
        "imei": "123456789012345",
        "status": "online",
        "latitude": -1.9403,
        "longitude": 29.8739,
        "last_seen": "2026-05-20T10:35:00Z"
      },
      "created_at": "2026-05-20T10:00:00Z"
    }
  ]
}
```

---

### `POST /api/payments/verify`

Step 8 of onboarding (for paid plans): Verifies Flutterwave transaction reference against Flutterwave's API. Creates a verified Payment record in the local database.

**Request Body:**
```json
{
  "txRef": "flw-tx-12345",
  "planId": "basic"
}
```

**Response (200):**
```json
{
  "verified": true,
  "status": "successful"
}
```

**Errors:**
- `400` Invalid planId (must be `basic` or `fleet`)
- `502` Flutterwave API unreachable

---

### `POST /api/subscriptions`

Step 8: Activates a subscription plan (`trial`, `basic`, or `fleet`).
- For paid plans, assumes `/api/payments/verify` was called successfully.
- For `trial`, enforces a one-time limit per user.
- Marks the user onboarding flow as complete (`onboarding_complete = true`).

**Request Body:**
```json
{
  "planId": "basic"
}
```

**Response (201):**
```json
{
  "subscriptionId": 12,
  "expiresAt": "2026-06-20T10:00:00Z"
}
```

**Errors:**
- `409` Trial already used (when trying to activate `trial` multiple times)

---

### `POST /api/subscriptions/upgrade`

Upgrades an existing active plan to a higher plan tier. Assumes `/api/payments/verify` was successfully called beforehand.

**Request Body:**
```json
{
  "planId": "fleet",
  "txRef": "flw-tx-12345"
}
```

**Response (201):**
```json
{
  "subscriptionId": 13,
  "expiresAt": "2026-06-20T10:00:00Z"
}
```

**Errors:**
- `400` Cannot downgrade (new plan price is lower than or equal to current plan)
- `402` Payment not verified or found

---

### `GET /api/billing`

Retrieves the current subscription status and past payment history for the user.

**Response (200):**
```json
{
  "currentPlan": "basic",
  "expiresAt": "2026-06-20T10:00:00Z",
  "payments": [
    {
      "txRef": "flw-tx-12345",
      "planId": "basic",
      "amount": 5000,
      "status": "successful",
      "createdAt": "2026-05-20T10:00:00Z"
    }
  ]
}
```

---

## WebSocket — Real-time Location Stream

**Endpoint:** `wss://byatron.tech/ws/locations/{device_id}?token=<clerk-session-token>`

Connect to receive real-time GPS location pushes the moment a packet arrives from the device. **Replaces polling `/api/locations/{device_id}/latest`.**

### Connection

```
wss://byatron.tech/ws/locations/1?token=<clerk-jwt>
```

The `token` query parameter is the same Clerk session JWT used for REST endpoints.

### Messages from Server → Client

**Location update** (sent on every GPS packet from the device):
```json
{
  "type": "location",
  "device_id": 1,
  "latitude": -1.9403,
  "longitude": 29.8739,
  "speed": 45.5,
  "course": 180,
  "timestamp": "2026-05-20T10:35:00Z",
  "gps_valid": true
}
```

**Alarm event** (sent when the device triggers an alarm):
```json
{
  "type": "alarm",
  "device_id": 1,
  "alarm_type": "SOS",
  "latitude": -1.9403,
  "longitude": 29.8739,
  "timestamp": "2026-05-20T10:35:00Z"
}
```

### Messages from Client → Server

Send any text string as a keep-alive ping every ~25 seconds to prevent NAT/proxy timeouts:
```
ping
```
The server does not reply to pings.

### Error Codes

| Code | Reason |
|---|---|
| `4001` | Unauthorized — token missing or invalid |
| `1000` | Normal closure |
| `1001` | Server going away |

### JavaScript / TypeScript Example

```typescript
const token = await clerkSession.getToken();
const ws = new WebSocket(
  `wss://byatron.tech/ws/locations/1?token=${encodeURIComponent(token)}`
);

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.type === "location") {
    console.log(`Device at ${msg.latitude}, ${msg.longitude} @ ${msg.speed} km/h`);
  }
  if (msg.type === "alarm") {
    console.warn(`ALARM: ${msg.alarm_type}`);
  }
};

// Keep-alive
setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send("ping"); }, 25_000);
```

### Nginx Configuration

If running behind nginx, ensure WebSocket upgrade headers are proxied:

```nginx
location /ws/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 300s;
}
```

---

## Dashboard (Web UI)

### `GET /dashboard`

Browser-based fleet overview. No authentication required. Accessible from any web browser.

**Displays:**
- All device names and IMEIs
- Online/offline status
- Battery level and GSM signal
- Last known coordinates
- Last update timestamp
- Speed and satellite count

---

## Error Reference

| HTTP Code | Meaning |
|---|---|
| `200 OK` | Success |
| `201 Created` | Resource created |
| `204 No Content` | Successful DELETE |
| `400 Bad Request` | Invalid input data |
| `401 Unauthorized` | Missing or invalid `Authorization` header |
| `403 Forbidden` | Valid token but insufficient permissions |
| `404 Not Found` | Resource does not exist |
| `409 Conflict` | Command failed (e.g. device not connected) |
| `422 Unprocessable Entity` | Malformed request body or missing required field |
| `503 Service Unavailable` | TCP server not running |
| `500 Internal Server Error` | Server-side error |

**Error body format:**
```json
{ "detail": "Human-readable error message" }
```

---

## Developer Notes

1. **All timestamps are UTC** in ISO 8601 format (`2026-05-20T10:35:00Z`)

2. **Pagination** — list endpoints support `skip` + `limit`

3. **Devices auto-register** when a GPS tracker first connects over TCP — no manual registration needed for hardware devices

4. **Real-time vs polling** — use the WebSocket endpoint for live tracking; the REST `/latest` endpoint is available for one-off reads

5. **Token expiry** — Clerk tokens expire. Reconnect the WebSocket with a fresh token on `4001` close code. The mobile SDK handles REST token refresh automatically

6. **CORS** — configured for all origins by default. Set `CORS_ORIGINS` in `.env` to restrict for production

7. **Rate limiting** — not currently implemented

---

## Architecture

```
Mobile App / Web Client
        │
        ├── REST (HTTPS)      →  FastAPI HTTP Server (port 8000)
        │                          ├── /api/auth     (open)
        │                          ├── /api/devices  🔒
        │                          ├── /api/locations 🔒
        │                          ├── /api/commands  🔒
        │                          ├── /api/trips     🔒
        │                          └── /dashboard     (open)
        │
        └── WebSocket (WSS)   →  /ws/locations/{device_id}?token=...  🔒
                                         │
                                         ▼
                                  PostgreSQL Database
                                         ▲
                                  TCP Server (port 7018)
                                         ▲
                                  GPS Tracker Devices
```

---

*Last updated: May 2026 — reflects WebSocket streaming, Clerk JWT authentication on all protected routes, and trip endpoints.*

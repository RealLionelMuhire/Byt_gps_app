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

**Admin dashboard** (`/admin/*`) uses a separate Clerk-based sign-in and a signed session cookie. Only users with `ADMIN` or `SUPER_ADMIN` role in the database can access it.

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

If the user was pre-provisioned by an admin via the dashboard's *Invite & Assign* flow (their row has a `pending_inv_...` placeholder `clerk_user_id`), this endpoint **claims** that row — rewriting it to the real Clerk user ID so device assignments made by the admin stay intact.

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
  "role": "USER",
  "onboarding_step": 0,
  "onboarding_complete": false,
  "created_at": "2026-06-12T10:00:00Z",
  "updated_at": "2026-06-12T10:00:00Z"
}
```

The first user ever created gets `role: "SUPER_ADMIN"`; all subsequent users default to `role: "USER"`. The role can be updated by an admin via `PUT /api/auth/users/{id}/role`.

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

### `GET /api/auth/users`
List all users. Requires `ADMIN` or `SUPER_ADMIN` role.

**Headers:** `Authorization: Bearer <token>`

**Query params:**
- `skip` (int, default 0) — pagination offset
- `limit` (int, default 100, max 500) — page size

**Response `200`:**
```json
[
  {
    "id": 1,
    "clerk_user_id": "user_2abc...",
    "email": "admin@example.com",
    "first_name": "Super",
    "last_name": "Admin",
    "role": "SUPER_ADMIN",
    "onboarding_complete": true,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-06-01T00:00:00Z"
  }
]
```

**Response `403`:** Caller does not have admin access.

---

### `PUT /api/auth/users/{id}/role`
Update a user's role. Requires `ADMIN` or `SUPER_ADMIN` role. Only `SUPER_ADMIN` can assign the `SUPER_ADMIN` role.

**Headers:** `Authorization: Bearer <token>`

**Request:**
```json
{
  "role": "ADMIN"
}
```

Valid roles: `SUPER_ADMIN`, `ADMIN`, `TECHNICIAN`, `USER`.

**Response `200`:** Full user object with updated role.

**Response `400`:** Invalid role name.

**Response `403`:** Insufficient permissions (e.g. non-SUPER_ADMIN trying to assign SUPER_ADMIN).

**Response `404`:** User not found.

**Response `409`:** Cannot demote the last remaining `SUPER_ADMIN`.

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

### `POST /api/payments/initiate`
Start an IntouchPay mobile money collection (Step 8, paid plans). This does NOT confirm payment — it dispatches a USSD approval prompt to the customer's phone and returns immediately. The payment is only confirmed later by `POST /api/webhooks/intouchpay` (or the cron reconciliation job).

**Request:**
```json
{
  "planId": "basic",
  "phone": "250781234567"
}
```

`planId` values are plan **slugs** — `trial`, `basic`, `fleet` by default, plus any custom schemes created by the admin (see Subscription Plans below). Prices/limits come from the admin-configured `subscription_plans` table.

**Response `200`:**
```json
{
  "txRef": "IP1a2b3c4d...",
  "status": "pending",
  "message": "Approve the payment on your phone to continue."
}
```

**Response `200` (rejected by IntouchPay):**
```json
{
  "txRef": "IP1a2b3c4d...",
  "status": "failed",
  "message": "Payment request was rejected."
}
```

---

### `POST /api/subscriptions`
Activate a subscription plan for the current user (Step 8). For paid plans, call after `/api/payments/initiate` and poll/retry this endpoint until the payment webhook has confirmed it. Free trial does not require payment.

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

**Response `402`:** Payment required — a successful payment record for this plan must exist before activating a paid subscription. Call `/api/payments/initiate` and wait for confirmation first.

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
**Response `400`:** Already on this plan. Choose a different plan to upgrade.  
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

## Subscription Plan Endpoints (`/api/subscription-plans`)

Admin-configurable subscription schemes. A plan defines a **billing type** (`one_time` = single payment, the length is how long it lasts; `recurrent` = recurring, the price is charged per length), a **price + currency** (e.g. `5000 RWF`), and a **length** (`duration_value` + `duration_unit`: day/week/month/year). Plans can be linked to GPS devices so each device's scheme is known.

### `GET /api/subscription-plans`
List subscription plans (auth required). The mobile app renders its pricing screen from this — **active plans only** by default.

**Query params:**
- `include_inactive` — admin only: also return deactivated plans (`true`)

**Response `200`:**
```json
[
  {
    "id": 1,
    "name": "Basic",
    "slug": "basic",
    "billing_type": "recurrent",
    "price": 5000,
    "currency": "RWF",
    "duration_value": 1,
    "duration_unit": "month",
    "duration_days": 30,
    "max_devices": 3,
    "description": "Monthly plan. Up to 3 vehicles.",
    "is_active": true
  }
]
```

### `POST /api/subscription-plans`
Create a subscription scheme. **Admin only.**

**Request:**
```json
{
  "name": "Silver Monthly",
  "slug": "silver",
  "billing_type": "recurrent",
  "price": 8000,
  "currency": "RWF",
  "duration_value": 1,
  "duration_unit": "month",
  "max_devices": 5,
  "description": "Monthly plan for small fleets."
}
```

**Response `201`:** The created plan object.  
**Response `409`:** A plan with that slug already exists.

### `PUT /api/subscription-plans/{plan_id}`
Partial update of a scheme. **Admin only.** Any subset of fields may be sent (name, billing_type, price, currency, duration_value, duration_unit, max_devices, description, is_active).

### `DELETE /api/subscription-plans/{plan_id}`
Soft-delete (deactivate) a scheme. **Admin only.** Deactivated plans disappear from the mobile pricing screen but stay attached to linked devices. Reactivate via `PUT` with `is_active: true`.

---

## Device Endpoints (`/api/devices`)

All require `Authorization: Bearer <token>`.

### `GET /api/devices/`
List all devices visible to the authenticated user.

**Query params:**
- `status` — filter by `online` or `offline`
- `lifecycle` — filter by `registered`, `in_stock`, or `sold`
- `owner_id` — admin only: filter by the client (user_id) the device is assigned to
- `skip` / `limit` — pagination (default limit: 100, max: 1000)

**Visibility rule:** non-admin users only ever see the devices assigned to their own account (`user_id == me`). Admins see the full inventory.

Each device object now carries a `subscription` block describing the owner's **payment scheme / subscription mode** (so the client's real billing state is never guessed at):

```json
{
  "id": 7,
  "imei": "358765012345678",
  "name": "Fleet Tracker #42",
  "plan": { "id": 2, "name": "Basic", "slug": "basic", "billing_type": "recurrent", "price": 5000, "currency": "RWF", "duration_value": 1, "duration_unit": "month", "duration_days": 30, "max_devices": 3, "description": null, "is_active": true },
  "subscription": {
    "status": "active",        // active | expired | none
    "plan_slug": "basic",
    "plan_name": "Basic",
    "billing_type": "recurrent", // one_time | recurrent
    "started_at": "2026-06-14T10:00:00Z",
    "expires_at": "2026-07-14T10:00:00Z"
  }
}
```

`subscription.status` is derived from the owner's latest subscription record: `active` = unexpired subscription, `expired` = subscription lapsed or cancelled, `none` = no subscription on file (trial pending or unpaid).

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

### `PUT /api/devices/{device_id}/plan`
**Admin only.** Link (or unlink) an admin-configured subscription scheme to a device — e.g. the scheme the client is billed for on this unit.

**Headers:** `Authorization: Bearer <token>` (ADMIN / SUPER_ADMIN)

**Request:**
```json
{ "plan_id": 2 }
```

Send `{ "plan_id": null }` to unlink.  
**Response `200`:** Full device object including `plan_id` and nested `plan`.
**Response `404`:** Device or plan not found.

---

### `POST /api/devices/{device_id}/assign`
**Admin only.** Assign an inventory device to a client account (role `USER`). One or many devices can be assigned per client, but a device can belong to **at most one client at a time** — assigning a device already owned by a *different* client returns `409`.

**Headers:** `Authorization: Bearer <token>` (ADMIN / SUPER_ADMIN)

**Request** — exactly one client identifier:
```json
{ "user_id": 42 }
```
```json
{ "clerk_user_id": "user_2abc123xyz456def" }
```
```json
{ "email": "client@example.com" }
```

**Response `200`:** Full device object with `user_id` set and `lifecycle: "sold"`.

**Response `400`:** Target account is not a client (role ≠ `USER`).  
**Response `404`:** Device or client not found.  
**Response `409`:** Device is already assigned to another client (unassign it first).

---

### `POST /api/devices/{device_id}/unassign`
**Admin only.** Reclaim a device from its client back to company stock. Clears `user_id`, resets `lifecycle` to `in_stock`, and issues a fresh pairing PIN for the next client. Idempotent (unassigning an unowned device is a no-op).

**Headers:** `Authorization: Bearer <token>` (ADMIN / SUPER_ADMIN)

**Response `200`:** Full device object with `user_id: null` and `lifecycle: "in_stock"`.

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

### `GET /api/devices/{device_id}/billing`
Full **payment scheme / subscription mode** picture for a single device — the linked plan, the owner's current subscription state, and the payment history that matches this device's plan. Owner or admin only.

**Response `200`:**
```json
{
  "device_id": 7,
  "imei": "358765012345678",
  "name": "Fleet Tracker #42",
  "lifecycle": "sold",
  "status": "online",
  "owner": {
    "user_id": 42,
    "name": "Jane Doe",
    "email": "jane@example.com"
  },
  "plan": { "id": 2, "name": "Basic", "slug": "basic", "billing_type": "recurrent", "price": 5000, "currency": "RWF", "duration_value": 1, "duration_unit": "month", "duration_days": 30, "max_devices": 3, "description": null, "is_active": true },
  "subscription": {
    "status": "active",
    "plan_slug": "basic",
    "plan_name": "Basic",
    "billing_type": "recurrent",
    "started_at": "2026-06-14T10:00:00Z",
    "expires_at": "2026-07-14T10:00:00Z"
  },
  "payments": [
    {
      "tx_ref": "FLW-TX-123456",
      "plan_id": "basic",
      "amount": 5000,
      "currency": "RWF",
      "status": "successful",
      "verified_at": "2026-06-14T10:05:00Z"
    }
  ]
}
```

Payments are the owner's records narrowed to this device's linked plan slug (all of the owner's payments when no plan is linked). Payments are created by the mobile app's IntouchPay payment flow — admins view the scheme, they never record payments manually.

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
Enable or disable the vibration/motion-sensor alarm (`SENALM`).

**Request:** `{ "enabled": true, "mode": 0 }` — `mode`: `0`=GPRS only, `1`=SMS+GPRS, `2`=GPRS+SMS+Call. Defaults to `0`.

---

### `POST /api/devices/{device_id}/alarm/power-cut`
Enable or disable the power-cut alarm (`POWERALM`) — fires when the device's
external power is disconnected. **Not** a battery-level alarm (this device
has no such command); the endpoint used to live at `/alarm/lowbattery` with
that (incorrect) description.

**Request:** `{ "enabled": true, "mode": 0, "detect_seconds": 5, "min_charge_seconds": 10 }`
`mode`: `0`=GPRS only, `1`=SMS+GPRS, `2`=GPRS+SMS+Call. `detect_seconds` (T1, `2-60`):
power-failure detection time. `min_charge_seconds` (T2, `1-3600`): minimum
charge time before re-arming.

---

### `POST /api/devices/{device_id}/alarm/ignition-on`
Enable or disable the ignition-**ON** alarm (`ACCALM`) — fires when ACC turns on.

**Request:** `{ "enabled": true, "mode": 0 }` — `mode`: `0`=GPRS, `1`=GPRS+SMS,
`2`=GPRS+Call, `3`=GPRS+SMS+Call. Defaults to `0`.

---

### `POST /api/devices/{device_id}/alarm/ignition-off`
Enable or disable the ignition-**OFF** alarm (`ACCOFFALM`) — fires when ACC
turns off. A separate, independently-armable alarm from ignition-on above —
not two states of one setting.

**Request:** same shape as `ignition-on`.

---

> **Removed 2026-09-01** (were never valid for the actual production
> hardware, G900LS J16-4G — see `docs/usage/CONFIGURATION_GUIDE.md`'s
> "Device 2" command reference): `POST /alarm/overspeed`,
> `POST /alarm/displacement`, `POST /alarm/sos`. None has a corresponding
> SMS command on this device — the previous `speed123456`/`move123456`/
> `KC123456` strings didn't correspond to anything in its command set. SOS
> has no software arm/disarm on this hardware at all (the physical button
> always calls/texts the `CENTER` admin numbers, independent of any
> setting); overspeed and displacement/geofence alarms aren't in this
> device's documented command set at all.

### `POST /api/devices/{device_id}/fuel/cut`
Cut power to the relay to immobilize the vehicle (`RELAY,1#`). Only safe
when speed < 20 km/h.

No request body required.

---

### `POST /api/devices/{device_id}/fuel/restore`
Restore power to the relay (re-enable vehicle) (`RELAY,0#`).

No request body required.

---

### `POST /api/devices/{device_id}/query/location`
Request current location from device via `WHERE#` (device replies via TCP).

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

Protected by Clerk-based session cookie. Only users with `ADMIN` or `SUPER_ADMIN` role in the database can access.

| Route | Description |
|---|---|
| `GET /admin/login` | Clerk sign-in page |
| `POST /admin/auth/verify` | Exchange Clerk JWT for session cookie |
| `GET /admin/logout` | Clear session, redirect to login |
| `GET /admin/devices` | Device inventory management UI (shows recent rejected connections; `?owner=` filters by assigned client) |
| `GET /admin/clients` | Client directory — every customer with their assigned devices & plan (`?q=` searches by name/email) |
| `GET /admin/devices/{imei}/billing` | Per-device payment scheme / subscription mode panel — linked plan (one-time vs recurrent, price, currency, length), owner's subscription state (active/expired/none + expiry), and payment history. Read-only (payments come from the IntouchPay payment flow). |
| `GET /admin/plans` | Subscription schemes — create, view, and activate/deactivate plans (mobile pricing + billing read from these) |
| `POST /admin/plans/create` | Create a new subscription scheme (one-time or recurrent, price, currency, length, max devices) |
| `POST /admin/plans/{plan_id}/toggle` | Activate / deactivate a plan |
| `POST /admin/devices/{imei}/plan` | Link or unlink a subscription scheme to a device from the inventory page |
| `POST /admin/devices` | Add a new device to the whitelist (accepts `sim_number`) |
| `POST /admin/devices/{imei}/assign` | Assign an unowned device to an existing client account by email |
| `POST /admin/devices/{imei}/assign-new` | Invite a NEW client (Clerk email invitation + local row, role USER) and assign the device in one step — the client sets their own password via the invite link |
| `POST /admin/devices/{imei}/verify`| Manually promote from `registered` to `in_stock` and send test command |
| `POST /admin/devices/{imei}/delete` | Remove an unpaired device |
| `POST /admin/devices/{imei}/unpair` | Reclaim a device from its client (clear owner, reset PIN) |
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

**Response `403`:** Valid token but user does not have `ADMIN` or `SUPER_ADMIN` role.

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
     │ {imei, name, sim_number}      │                           │
     │ ──────────────────────►DB     │                           │
     │ (State: registered)           │                           │
     │                               │ TCP Login (IMEI)          │
     │                               │ ──────────────────────►   │
     │                               │  Server checks whitelist  │
     │                               │  ✅ Auto-promotes to      │
     │                               │     in_stock              │
     │                               │                           │
     │                               │                 POST /api/devices/pair
     │                               │                 {imei, pairingPin}
     │                               │                 ──────────────────►DB
     │                               │                 ✅ State -> sold
     │                               │                           │
     │                               │                 GET /api/devices/{imei}/status
     │                               │                 ← {status: "online"}
```

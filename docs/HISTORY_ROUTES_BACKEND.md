# Trip / Route History — Backend Investigation

Read-only investigation of how historical location and trip data is served
today. Reflects `server/app/` as of commit `4f565c2` (2026-08-02,
"Enforce device ownership across locations, commands, trips, and devices
APIs"). No backend code was changed as part of this investigation.

Source files:
- `server/app/api/locations.py`
- `server/app/api/trips.py`
- `server/app/api/devices.py` (one trip-related route)
- `server/app/models/location.py`, `models/trip.py`, `models/trip_settings.py`
- `server/app/services/trip_detection.py`, `services/trip_service.py`
- `server/app/core/auth.py`
- `server/app/api/ws.py` (for schema comparison)

---

## 1. Endpoints

All routes below require `Depends(get_current_user)` (Clerk-authenticated
session → local `User` row). Router prefixes: `locations.router` →
`/api/locations`, `trips.router` → `/api/trips`, `devices.router` →
`/api/devices` (registered in `main.py:137-143`).

### Location history — `/api/locations/*`

| Method & Path | Query params | Response |
|---|---|---|
| `GET /{device_id}/latest` | — | Single `LocationResponse` (most recent point, any validity) |
| `GET /{device_id}/history` | `start_time`, `end_time` (ISO-8601 datetime, optional), `limit` (int, default 1000, 1–10000) | `LocationHistoryResponse`: `device_id`, `device_name`, `device_imei`, `total_points` (count matching filter, ignores `limit`), `locations: [LocationResponse]` |
| `GET /{device_id}/route` | `start_time`, `end_time` (optional), `simplify` (bool, default `False`) | GeoJSON `FeatureCollection` of `Point` features, one per GPS-valid location, each with `speed`/`course`/`timestamp`/`is_alarm` properties, plus a top-level `properties` block (`device_id`, `device_name`, `start_time`, `end_time`, `point_count`) |
| `GET /{device_id}/distance` | `start_time`, `end_time` (optional) | `DistanceResponse`: `device_id`, `device_name`, `device_imei`, `start_time`, `end_time`, `point_count`, `total_distance_km` |
| `GET /{device_id}/route-line` | `start_time`, `end_time` (optional) | `RouteLineStringResponse`: GeoJSON-flavored `LineString` — `type`, `coordinates: [[lon,lat],...]`, parallel arrays `timestamps`, `speeds`, `courses`, plus `properties` (`start_time`, `end_time`, `point_count`, `device_id`, `device_name`, `device_imei`) |
| `GET /{device_id}/alarms` | `start_time`, `end_time` (optional), `limit` (default 100, 1–1000) | `List[LocationResponse]`, filtered to `is_alarm == True` |
| `GET /nearby` | `latitude`, `longitude` (required), `radius_km` (default 10, 0.1–100) | Ad hoc dict; **not device-scoped, no auth dependency at all** (see §4) |

Date range behavior (consistent across `history`, `route`, `distance`,
`route-line`, `alarms`):
- `start_time`/`end_time` are plain FastAPI `datetime` query params (ISO-8601
  strings, e.g. `2026-08-01T00:00:00`), no explicit timezone handling —
  compared directly against the naive `DateTime` column.
- If `start_time` omitted: defaults to `now - 24h` (`now - 7d` for `/alarms`).
- If `end_time` omitted: defaults to `now` (or left as `None` and the DB
  query simply has no upper bound, per-endpoint — see code for exact spot).
- No cursor/offset pagination anywhere. `history` and `alarms` use a flat
  `limit`; `route`/`route-line`/`distance` have **no limit at all** — they
  return every matching row for the range.

`simplify` on `/route` is a **declared but dead parameter** — it's accepted
in the signature but never read anywhere in the function body. Passing
`simplify=true` has no effect on the output today.

Shared helpers used by both `locations.py` and `trips.py`:
- `verify_device_access(device_id, user, db)` — 404s if device missing, then
  calls `require_device_access`.
- `compute_distance_for_device_time_range(...)` — Haversine sum over
  GPS-valid points in range.
- `fetch_route_line_for_range(...)` — builds the same LineString structure
  used by `/route-line` and by `GET /api/trips/{trip_id}`.

### Trips — `/api/trips/*`

`Trip` is a persisted, user-created "saved trip" over a time range — see
§2 for whether this is auto-detected or not.

| Method & Path | Query/body params | Response |
|---|---|---|
| `GET /settings` | — | `TripSettingsResponse` (per-user trip segmentation prefs) |
| `PUT /settings` | body: `TripSettingsUpdate` (all fields optional) | `TripSettingsResponse` |
| `GET /suggested` | `device_id` (required), `start_time`, `end_time` (optional) | `List[SuggestedTripResponse]` — stop-duration-based segments, **not persisted** |
| `POST /start` | body: `TripStartRequest{device_id, name}` | `TripResponse`, 201. Creates a Trip row with `end_time=NULL` (active trip). 400 if device already has an active trip. |
| `POST ""` (create) | body: `TripCreate{device_id, name, start_time, end_time}` | `TripResponse`, 201. Computes distance + reverse-geocodes a `display_name` from existing location history in the given range. 400 if no GPS-valid points found in range. |
| `GET ""` (list) | `device_id` (required) | `List[TripResponse]`, ordered by `created_at desc` |
| `GET /{trip_id}` | `device_id` (required) | `TripDetailResponse` = `TripResponse` + `device_name`, `device_imei`, `route` (the same LineString dict `fetch_route_line_for_range` produces). For an active trip (`end_time IS NULL`), `end_time` for route-fetching purposes falls back to the last known GPS-valid location's timestamp, or `now()` if none exists. |
| `POST /{trip_id}/end` | `device_id` (required) | `TripResponse`. Manually ends an active trip via `end_active_trips_for_device`; no-op (200, returns as-is) if already ended. |
| `DELETE /{trip_id}` | `device_id` (required) | 204. Deletes the `Trip` row only — underlying `Location` rows are untouched. |

Additionally, `GET /api/devices/{device_id}/trips` (`devices.py:156`) lists
trips for a device — a second entry point into the same `Trip` table,
separate router, same `TripResponse` shape, same ownership check.

`device_id` is required on nearly every trip route (not inferred from the
trip row) — it's used both as an ownership-check anchor and as a filter on
the `Trip` query itself.

---

## 2. Is there a real "trip" concept, or is it derived client-side?

**Both exist, doing different things:**

- **`trips` table** (`models/trip.py`) — a real persisted trip with defined
  `start_time` / `end_time` (nullable = "still active"), `device_id`,
  `user_id`, `total_distance_km`, optional `start_location_id` /
  `end_location_id` FKs into `locations`, and a geocoded `display_name`.
  Trips are created three ways:
  - `POST /api/trips/start` — explicit start, no end time yet.
  - `POST /api/trips` — retroactively wraps an existing time range picked
    by the caller (client already knows the boundaries).
  - Auto-close: `main.py` runs a background stale-checker
    (`end_active_trips_for_device`, called on stale `last_update`/
    `last_connect`) that ends active trips when a device stops sending.
    This is the only fully server-driven trip lifecycle path.

- **Boundary *detection* is a separate, non-persisted feature**:
  `GET /api/trips/suggested` → `services/trip_detection.py:detect_trip_segments`.
  This walks raw `Location` rows for a device/time-range and splits them
  into segments wherever a "stopped" run (`speed < stop_speed_threshold_kmh`,
  default 5 km/h) lasts ≥ `stop_splits_trip_after_minutes` (default 60 min),
  dropping segments shorter than `minimum_trip_duration_minutes` (default
  5 min). These thresholds are per-user, stored in `trip_settings`
  (`GET`/`PUT /api/trips/settings`).
  **`SuggestedTrip` segments are computed on the fly and never written to
  the `trips` table** — the client has to call `POST /api/trips` separately
  if it wants to persist one of the suggestions.

So: trip boundaries are not purely a client-side responsibility (there's a
real segmentation algorithm server-side), but it's opt-in and read-only —
nothing auto-populates the `trips` table from detected segments. A caller
that only ever calls the raw `/locations/*` endpoints would indeed have to
do its own boundary detection.

---

## 3. Polyline simplification

**None exists.** Grepped the whole `server/app` tree for
Douglas-Peucker/RDP/decimation logic — no hits.

- `/route-line` and `GET /api/trips/{trip_id}`'s `route` field both go
  through `fetch_route_line_for_range` (`locations.py:61-97`), which just
  maps every matching `Location` row 1:1 into `coordinates`/`timestamps`/
  `speeds`/`courses` arrays, in timestamp order. No point is dropped.
- `/route` (GeoJSON `FeatureCollection`) has a `simplify: bool` query
  parameter that **looks like** a simplification toggle but is unused dead
  code (see §1) — it doesn't reduce point count regardless of value.
- The only reduction of point count anywhere in this path is the `limit`
  param on `/history` (hard cap, not geometric simplification) and the
  implicit `gps_valid == True` filter applied by `route`, `route-line`,
  `distance`, and trip routes (invalid fixes are excluded, not decimated).

Net effect: for a long time range, `/route-line` and trip `route` payloads
grow linearly with the number of stored points — a client requesting a
multi-day range gets every single raw point back.

---

## 4. RBAC / ownership enforcement — current state

Confirmed current as of commit `4f565c2` ("Enforce device ownership across
locations, commands, trips, and devices APIs", 2026-08-02 23:46), which
directly touched `locations.py` and `trips.py`. Prior state is documented
in `docs/RBAC_CURRENT_STATE.md` (dated the same day, pre-fix) — that doc
explicitly flagged `locations.py`'s `verify_device_access` as ownership-
disabled and `trips.py` as using a fake `get_default_user()` stand-in
instead of the real caller. Both problems are fixed in the current code:

- **`server/app/core/auth.py`**: `require_device_access(device, user)` is
  the shared check — raises `404` (not `403`) if the caller neither owns
  the device (`device.user_id == user.id`) nor holds an admin role
  (`SUPER_ADMIN`/`ADMIN`, via `user_can_access_device`). 404 (rather than
  403) is deliberate, so a non-owner can't distinguish "device doesn't
  exist" from "device isn't yours."
- **`locations.py`**: every device-scoped route (`latest`, `history`,
  `route`, `distance`, `route-line`, `alarms`) calls
  `verify_device_access(device_id, user, db)` before touching data — this
  local wrapper 404s if the device row is missing, then delegates to
  `require_device_access`. Confirmed by reading every handler in the file.
  - Exception: `GET /nearby` has no auth dependency and is not
    device-scoped (returns any device with a last-known position within a
    radius) — this predates and is outside the scope of the ownership fix;
    still open.
- **`trips.py`**: every route now depends on `get_current_user` (the real
  authenticated caller) and calls `verify_device_access(body.device_id / 
  device_id, user, db)` — `start_trip`, `create_trip`, `list_trips`,
  `get_trip`, `end_trip_manually`, `delete_trip`, `get_suggested_trips`.
  `get_default_user()` is gone from this file. `settings` routes
  (`GET`/`PUT /settings`) are scoped to `user.id` directly (no device
  involved, nothing to own-check).
- **`devices.py`**: `GET /{device_id}/trips` independently re-checks
  ownership (looks up the device, 404s if missing, then calls
  `require_device_access` directly) rather than delegating to
  `verify_device_access` — same effective behavior, just not routed
  through the locations.py helper.

**Conclusion: yes, the endpoints in scope (`/api/locations/*` device
routes and `/api/trips/*`) now correctly enforce owner-or-admin access.**
The one gap that remains is `/api/locations/nearby`, which was out of
scope for the RBAC commit and has no ownership model to enforce (it's a
cross-device spatial query, not device-scoped) — but it also has **no auth
dependency at all**, unlike every other route in the file, which is worth
flagging separately if it's meant to be gated.

---

## 5. Location point schema

### `LocationResponse` (REST — `latest`, `history`, `alarms`)

From `models/location.py` / `locations.py:101-116`, `from_attributes = True`
directly off the `Location` ORM row:

```python
id: int
device_id: int
latitude: float
longitude: float
speed: float          # km/h, default 0
course: int            # degrees 0-360, default 0
satellites: int        # default 0
gps_valid: bool         # default False
is_alarm: bool          # default False
alarm_type: Optional[str]
timestamp: datetime     # GPS tracker's own clock
received_at: datetime   # server receive time
```

Also present on the model but **not exposed** in `LocationResponse`: `geom`
(PostGIS `Geometry('POINT', srid=4326)`, populated alongside
`lat`/`lon` for spatial queries but never serialized out).

### `/route` (GeoJSON) and `/route-line` / trip `route`

Reduced field sets, not the full `LocationResponse`:
- `/route` feature `properties`: `timestamp`, `speed`, `course`, `is_alarm`
  (drops `satellites`, `gps_valid` — implied true by the `gps_valid==True`
  filter, `alarm_type`, `id`, `received_at`).
- `/route-line` / trip `route`: parallel arrays only carry `timestamps`,
  `speeds`, `courses` alongside `coordinates` — no `satellites`,
  `is_alarm`, `alarm_type`, `gps_valid`, `id`, or `received_at` at all.

### WebSocket live stream (`ws.py`, `/ws/locations/{device_id}` and fleet-wide variant)

Per `ws.py:199-217` docstring, the live push envelope is:

```json
{ "type": "location", "device_id": int,
  "latitude": float, "longitude": float,
  "speed": float, "course": int,
  "timestamp": str (ISO-8601), "gps_valid": bool }
```

with a separate envelope for alarms:

```json
{ "type": "alarm", "device_id": int, "alarm_type": str,
  "latitude": float, "longitude": float, "timestamp": str (ISO-8601) }
```

**Comparison to REST `LocationResponse`:** the WS location envelope is a
subset — same field *names* for the fields it does carry (`latitude`,
`longitude`, `speed`, `course`, `timestamp`, `gps_valid`, `device_id`), but
it omits `id`, `satellites`, `received_at`, `is_alarm`, `alarm_type` (those
only appear in the separate `"type": "alarm"` message), and adds a
`"type"` discriminator field the REST responses don't have. A client
switching between live WS updates and REST history/route data is dealing
with three overlapping-but-not-identical shapes: full (`LocationResponse`),
route-reduced (`/route`, `/route-line`), and WS-live — none of them
byte-for-byte the same.

---

## Summary for anyone building a history/route UI on top of this

- All read endpoints are ownership-safe now except `/api/locations/nearby`
  (unauthenticated, cross-device).
- Use `/route-line` (or a trip's `route` field) for map rendering — it's
  the most compact shape, but it is **raw, unsimplified, unlimited-length**
  data; a multi-day range will return every stored point. If you need
  point-count control, you currently have to do it client-side or add
  server-side simplification (the `simplify` flag on `/route` is a no-op
  today, not a working feature to lean on).
- `GET /api/trips/suggested` gives you server-computed trip boundaries for
  free (stop-duration heuristic, tunable via `/api/trips/settings`), but
  you must explicitly `POST /api/trips` to persist a chosen suggestion —
  nothing does that automatically except the "device went stale" auto-close
  path for trips already `POST /api/trips/start`-ed.
- Don't assume WS-live and REST-history points share one schema — map them
  through a shared client-side type rather than assuming field-for-field
  parity.

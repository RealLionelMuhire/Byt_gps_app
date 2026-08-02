# RBAC Current State (Backend Audit)

Investigation snapshot of the backend's role-based access control, done to
inform role-aware behavior in the Flutter client. Investigation only — no
backend code was changed. Reflects `server/app/` as of 2026-08-02.

## 1. Roles that exist

Defined in `server/app/models/user.py`:

```python
class Role(str, enum.Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    TECHNICIAN = "TECHNICIAN"
    USER = "USER"
```

`User.role` is a non-nullable enum column, default `USER`. There's also a
convenience property `user.is_admin` → `True` for `SUPER_ADMIN` or `ADMIN`.

**How role is set / changes:**
- **The first user ever created becomes `SUPER_ADMIN` automatically** (both
  in `POST /api/auth/sync` and `POST /api/auth/admin-create-user`, via
  `db.query(User).count() == 0`). Every user after that defaults to `USER`.
- SUPER_ADMIN/ADMIN users get `onboarding_complete` force-set to `True` on
  sync (they skip the customer onboarding flow).

## 2. How does a user become admin?

**Two ways, not one:**

1. `POST /api/auth/admin-create-user` — creates a *brand new* user in Clerk
   + DB. Protected by `X-Admin-Secret` header. This only grants
   `SUPER_ADMIN` if it happens to be the very first user ever; otherwise
   every user created this way is `USER`. **It's not actually an
   "elevate to admin" endpoint** — it's a user-creation endpoint that
   doesn't accept a role parameter at all.
2. **`PUT /api/auth/users/{id}/role`** (`auth.py:358`) — the real
   role-change mechanism. It exists and is live:
   - Requires `require_admin` (ADMIN or SUPER_ADMIN caller).
   - Only a `SUPER_ADMIN` can *assign* `SUPER_ADMIN` to someone else.
   - Only a `SUPER_ADMIN` can change another `SUPER_ADMIN`'s role.
   - Blocks self-demotion if you're the last `SUPER_ADMIN` (409).
   - Body: `{"role": "ADMIN"}` etc., validated against the `Role` enum.

So: role changes happen via `PUT /api/auth/users/{id}/role`, gated by
whoever already holds ADMIN/SUPER_ADMIN — there's no self-service
promotion path.

## 3. Does `GET /api/auth/me` return role?

**Yes.** `UserResponse` (used by both `/me` and `/sync`) includes
`role: str` directly:

```python
class UserResponse(BaseModel):
    id: int
    ...
    role: str
    ...
```

The Flutter app gets the role as a plain string (`"USER"`, `"ADMIN"`,
`"TECHNICIAN"`, `"SUPER_ADMIN"`) right after calling `POST /api/auth/sync`
or `GET /api/auth/me` — no separate lookup needed.

## 4. Do core data endpoints filter by ownership / distinguish admin?

Mixed and inconsistent — this is the part that needs care.

| Endpoint | Ownership/role-aware? |
|---|---|
| `GET /api/devices/` (list) | **Yes** — non-admin users are filtered to `Device.user_id == user.id`; ADMIN/SUPER_ADMIN see all devices. This is the one place the distinction is actually implemented. |
| `GET /api/devices/{id}`, `GET /api/devices/imei/{imei}` | **No** — no ownership check at all; any authenticated user can fetch any device by ID/IMEI. |
| `PUT /api/devices/{id}`, `DELETE /api/devices/{id}` | **No** — any authenticated user can edit/delete *any* device, not just their own, and no admin check either. |
| `GET /api/locations/{device_id}/latest\|history\|route\|distance\|route-line\|alarms` | **No** — all route through `verify_device_access()` in `locations.py:28`, whose docstring literally says *"Ownership check disabled (no Clerk auth)"* — it only checks the device exists, not who owns it. Any authenticated user can pull any device's location history/route. |
| `GET/POST /api/trips/*` (nearly all of it) | **No auth dependency at all** on most routes (`list_trips`, `create_trip`, `get_trip`, `delete_trip`, `start_trip`, settings, suggested). They use a `get_default_user()` helper that just grabs `db.query(User).first()` — this looks like leftover pre-auth scaffolding, not real multi-tenant logic. |
| `POST /api/devices/{id}/command`, `/alarm/*`, `/fuel/*` (commands.py) | `require_auth` only — no ownership or role check; any logged-in user can send commands to any device ID. |

So today: **admin vs. regular user is only distinguished in one place**
(`GET /api/devices/`). Everywhere else, "auth" means "any logged-in user" —
ownership isn't enforced, and a regular USER can currently reach another
user's device/location/trip data by guessing/incrementing IDs. This is a
backend gap, not just a Flutter concern, and is worth raising separately.

## 5. Admin-only fleet-wide endpoints

- **`GET /api/auth/users`** (`auth.py:344`) — confirmed admin-only
  (`require_admin`), returns all users, paginated.
- **`PUT /api/auth/users/{id}/role`** — admin-only, as above.
- **No JSON API equivalent exists for devices/trips/locations.** The only
  fleet-wide *device* view is `GET /dashboard` and `GET /admin/devices` in
  `server/app/dashboard.py` — but that's a server-rendered HTML admin panel
  with its own cookie-based session auth (separate from the Clerk JWT flow
  the mobile app uses), not a JSON endpoint the Flutter app can call.
- If "admin sees all devices" is needed in the app beyond the list view,
  that's already available for free from `GET /api/devices/` (role-filtered
  server-side). There's no admin fleet-wide trips/locations endpoint —
  that would need to be built.

## Bottom line for the Flutter client

- **Trust `/api/auth/me`'s `role` field** — it's reliable and returned
  today.
- **Role-gated UI is safe to build** (show/hide admin screens, the
  user-management screen hitting `GET /api/auth/users` +
  `PUT /api/auth/users/{id}/role`) — those two admin endpoints are properly
  protected server-side.
- **Don't assume the backend enforces ownership everywhere.** For
  devices/locations/trips beyond the top-level list, a regular user's app
  could currently fetch another user's data if it had the ID, because the
  backend doesn't check. If the Flutter app is the only client hitting
  these endpoints, this is "worked around by the app never requesting
  other users' IDs," but it is not a real security boundary — flag it to
  whoever owns the backend before relying on it for anything sensitive.

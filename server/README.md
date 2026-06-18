# Track IQ — GPS Tracking Server

FastAPI-based GPS tracking backend for the **Track IQ** mobile application. Handles real-time GPS hardware communication over TCP and exposes a secure REST API consumed by the Android/iOS app.

**Live API:** `https://api.track-iq.tech`  
**Interactive Docs:** `https://api.track-iq.tech/docs`  
**Admin Dashboard:** `https://api.track-iq.tech/admin/login`  
**Fleet Monitor:** `https://api.track-iq.tech/dashboard`

---

## Architecture

```
GPS Tracker Hardware
        │  TCP binary protocol
        ▼ port 7018
  ┌─────────────┐
  │  TCP Server │  ← Validates IMEI whitelist, rejects unknown devices
  └──────┬──────┘
         │
         ▼
  ┌──────────────────┐
  │  Neon PostgreSQL  │  (Serverless — PostGIS enabled)
  │  + PostGIS        │
  └──────┬───────────┘
         │
         ▼
  ┌──────────────┐
  │  FastAPI App │  ← REST API + WebSocket + Admin Dashboard
  └──────┬───────┘
         │  HTTPS  port 443 (Nginx → 8001)
         ▼
  Track IQ Mobile App (Clerk JWT auth)
```

---

## Features

| Feature | Status |
|---|---|
| TCP server for GPS hardware | ✅ Live |
| Binary protocol parser (0x7878) | ✅ Live |
| Clerk JWT authentication | ✅ Live |
| User registration & profile sync | ✅ Live |
| Device whitelist + Pairing PIN | ✅ Live |
| Vehicle management | ✅ Live |
| Real-time location via WebSocket | ✅ Live |
| Trip detection & history | ✅ Live |
| Subscription & billing (Flutterwave) | ✅ Live |
| Admin web dashboard (Clerk login) | ✅ Live |
| Neon serverless PostgreSQL | ✅ Live |
| PostGIS spatial queries | ✅ Live |

---

## Quick Start (Docker — Recommended)

### Prerequisites
- Docker & Docker Compose installed
- A [Clerk](https://clerk.com) project with dev/live API keys
- A [Neon](https://neon.tech) serverless PostgreSQL database

### 1. Configure environment

```bash
cd server/
cp .env.example .env
nano .env   # Fill in the values below
```

**Minimum required `.env` for local development:**

```ini
DEBUG=True

# Neon serverless DB (enable PostGIS first — server does this automatically)
DATABASE_URL=postgresql://user:password@ep-xxx.neon.tech/neondb?sslmode=require

# Security
SECRET_KEY=run-openssl-rand-hex-32-and-paste-here

# Clerk Authentication
CLERK_SECRET_KEY=sk_test_...
CLERK_PUBLISHABLE_KEY=pk_test_...
# Comma-separated Clerk User IDs allowed to access /admin
ADMIN_CLERK_USER_IDS=user_2abc123

# Payments (optional for local dev)
FLUTTERWAVE_SECRET_KEY=FLWSECK-...
```

### 2. Start the server

```bash
docker compose up --build
```

Server will be available at:
- HTTP API: `http://localhost:8001`
- Swagger UI: `http://localhost:8001/docs`
- Admin login: `http://localhost:8001/admin/login`
- Fleet dashboard: `http://localhost:8001/dashboard`

---

## Development (Without Docker)

```bash
cd server/
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload --port 8000
```

---

## Deployment (DigitalOcean + Nginx)

The production server runs at `api.track-iq.tech` on a DigitalOcean Droplet.

### Deploy new code

```bash
# From the project root on your local machine:
./deployment.sh

# Or force a clean rebuild:
./deployment.sh --no-cache
```

The deployment script:
1. Pulls latest code from `git origin/main`
2. Builds a new Docker image
3. Replaces the running container (zero-downtime)
4. Cleans up old images

### Nginx configuration

Located at `/etc/nginx/sites-available/api.track-iq.tech` on the server.  
Proxies `https://api.track-iq.tech` → `http://localhost:8001`.  
SSL certificate managed by Certbot (auto-renews).

---

## Security Architecture

### GPS Device Whitelisting

Devices **must** be pre-registered in the database before they can connect. The TCP server **rejects** unknown IMEIs immediately.

When a device is added via the admin dashboard, a random **Pairing PIN** is generated and shown once. This PIN must be entered in the mobile app when pairing — preventing IMEI hijacking.

### API Authentication

All `/api/*` endpoints require a valid **Clerk JWT** in the `Authorization: Bearer <token>` header. The server validates the JWT signature against Clerk's JWKS endpoint.

### Admin Dashboard Authentication

`/admin/*` routes use a Clerk-based sign-in. After successful Clerk authentication, the server checks the user's Clerk ID against `ADMIN_CLERK_USER_IDS`. If authorized, a signed session cookie (HMAC-SHA256) is issued for 8 hours.

---

## Environment Variables Reference

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✅ | Neon (or any PostgreSQL) connection string |
| `SECRET_KEY` | ✅ | Random 32-byte hex string for signing session cookies |
| `CLERK_SECRET_KEY` | ✅ | Clerk backend secret (`sk_live_...` or `sk_test_...`) |
| `CLERK_PUBLISHABLE_KEY` | ✅ | Clerk frontend key (`pk_live_...`) — for admin login page |
| `ADMIN_CLERK_USER_IDS` | ✅ | Comma-separated Clerk User IDs with admin access |
| `FLUTTERWAVE_SECRET_KEY` | For payments | Flutterwave secret key for payment verification |
| `ADMIN_SECRET` | Optional | Legacy header secret for `POST /api/auth/admin-create-user` |
| `DEBUG` | Optional | `True` for local dev (disables HTTPS-only cookies) |
| `LOG_LEVEL` | Optional | `INFO` (default) or `DEBUG` |
| `TCP_PORT` | Optional | TCP port for GPS hardware (default: `7018`) |
| `HTTP_PORT` | Optional | Internal HTTP port (default: `8000`) |

---

## Database Migrations

Migrations are plain SQL files in `migrations/`. Run them in order on first deployment:

```bash
# SSH into server then:
psql $DATABASE_URL -f server/migrations/001_add_clerk_auth.sql
psql $DATABASE_URL -f server/migrations/002_add_trips_table.sql
# ... up to the latest migration number
psql $DATABASE_URL -f server/migrations/008_add_pairing_pin.sql
```

On a fresh Neon database, the server's `init_db()` creates all tables automatically on first start (PostGIS is also enabled automatically).

---

## Monitoring

```bash
# Live server health
curl https://api.track-iq.tech/health

# View Docker logs on server (SSH in first)
docker logs gps_tracking_server -f

# Active TCP connections from trackers
curl https://api.track-iq.tech/health | jq .tcp_connections
```

---

## GPS Tracker Configuration

To register a physical tracker with the server, configure it with the server IP and TCP port:

```
SERVER: api.track-iq.tech  (or raw IP: 164.92.x.x)
PORT:   7018
```

> **Important:** The tracker's IMEI must be whitelisted in the admin dashboard first. Unknown IMEIs are rejected at the TCP layer.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI + Uvicorn |
| Database | PostgreSQL (Neon Serverless) + PostGIS |
| ORM | SQLAlchemy |
| Authentication | Clerk (JWT/JWKS) |
| Payments | Flutterwave |
| Reverse proxy | Nginx + Certbot SSL |
| Containerisation | Docker + Docker Compose |
| Deployment | DigitalOcean Droplet |

---

**Track IQ GPS Server — Ready to track! 🚀📍**

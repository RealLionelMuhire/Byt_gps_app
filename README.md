# Track IQ — GPS Tracking Application

A complete GPS tracking solution supporting **TK903ELE** and **G900LS J16-4G** GPS trackers with real-time monitoring, REST API, trip management, and location history.

**Production API:** `https://api.track-iq.tech`  
**GPS TCP Port:** `7018`  
**API Docs (Swagger):** `https://api.track-iq.tech/docs`

## 🚀 Features

- **Real-time GPS tracking** — Track multiple devices simultaneously via WebSocket
- **TCP Server** — Receives GT06 binary protocol data on port 7018
- **REST API** — Complete API for device and location management
- **Trip Management** — Auto-detected trips, route playback, distance calculations
- **Battery & Signal Monitoring** — Track device battery level and GSM signal strength
- **Location History** — Store and query historical GPS data with time filters
- **Geofencing & Alarms** — SOS, vibration, overspeed, ACC, power-cut alarms
- **Remote Commands** — Send commands to devices over existing TCP connection (no SMS needed)
- **Clerk Authentication** — Multi-user support with Clerk JWT integration
- **Admin Dashboard** — Web UI for device inventory management
- **Subscription Plans** — Trial / Basic / Fleet plans with Flutterwave payments
- **Docker Deployment** — One-command setup with Docker Compose

## 🔐 Authentication

All `/api/*` endpoints (except `/api/auth/sync`) require a Clerk Bearer JWT:

```javascript
// Sync user after Clerk sign-in (no auth header needed)
await fetch('https://api.track-iq.tech/api/auth/sync', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    clerk_user_id: user.id,
    email: user.emailAddresses[0].emailAddress,
    first_name: user.firstName,
    last_name: user.lastName,
  }),
});

// All subsequent API requests use Bearer token
await fetch('https://api.track-iq.tech/api/devices/', {
  headers: { 'Authorization': `Bearer ${clerkSessionToken}` },
});
```

### Quick Links
- **[API Reference](server/API_ENDPOINTS.md)** — Full endpoint documentation
- **[Device Config Guide](docs/usage/CONFIGURATION_GUIDE.md)** — TK903ELE & G900LS setup
- **[Deployment Checklist](DEPLOYMENT_CHECKLIST.md)** — Step-by-step deployment
- **[Architecture Diagrams](server/ARCHITECTURE_DIAGRAM.md)** — Visual flow diagrams

## 📁 Project Structure

```
Byt_gps_app/
├── server/                    # GPS tracking server
│   ├── app/                   # FastAPI application
│   │   ├── api/              # REST API endpoints
│   │   ├── core/             # Configuration & database
│   │   ├── models/           # SQLAlchemy models
│   │   ├── main.py           # Application entry
│   │   ├── tcp_server.py     # TCP server for GPS trackers
│   │   └── protocol_parser.py # Binary protocol parser
│   ├── docker-compose.yml    # Docker setup
│   ├── Dockerfile            # Container image
│   ├── requirements.txt      # Python dependencies
│   ├── README.md            # Server documentation
│   ├── DEPLOYMENT.md        # Deployment guide
│   ├── LOCAL_SETUP.md       # Local testing guide
│   └── QUICK_REFERENCE.md   # Quick commands
│
├── device_info.py            # Monitor GPS tracker via USB
├── gps_config.py            # Configure GPS tracker
├── test_connection.py       # Test device connection
├── analyze_protocol.py      # Protocol analysis tool
│
├── docs/                    # Documentation
│   ├── usage/              # Usage guides
│   └── installation/       # Installation guides
│
└── Architecture.md         # System architecture

```

## 🏠 Quick Start - Local Testing

### 1. Clone the Repository

```bash
git clone https://github.com/YOUR_USERNAME/Byt_gps_app.git
cd Byt_gps_app
```

### 2. Start Server Locally

```bash
cd server
docker compose up -d
```

### 3. Verify Server is Running

```bash
curl http://localhost:8000/health
```

Expected response:
```json
{"status":"healthy","tcp_connections":0,"active_devices":0}
```

### 4. Configure GPS Tracker

Find your local IP:
```bash
ip addr show | grep "inet " | grep -v 127.0.0.1
```

Configure device:
```bash
sudo ./gps_config.py
# Enter your local IP (e.g., 192.168.0.6)
# Port: 7018
# APN: internet
```

### 5. Monitor Device

```bash
sudo ./device_info.py
```

### 6. Check API

Open browser: http://localhost:8000/docs

## 🚀 Production Deployment

### Prerequisites

- Ubuntu 24.04 LTS server
- Docker and Docker Compose installed
- Ports 7018 (TCP) and 8000 (HTTP) open
- Domain: `api.track-iq.tech`

### Deploy

```bash
# 1. SSH to your server
ssh root@your-server-ip

# 2. Clone repository
git clone https://github.com/YOUR_USERNAME/Byt_gps_app.git
cd Byt_gps_app/server

# 3. Run automated setup
chmod +x setup.sh
sudo ./setup.sh

# 4. Verify deployment
curl https://api.track-iq.tech/health
```

### Configure GPS Tracker for Production

**TK903ELE (SMS):**
```
SERVER,0,api.track-iq.tech,7018,0#
APN,internet,,#
```

**G900LS J16-4G (SMS):**
```
SERVER,1,api.track-iq.tech,7018,0#
APN,internet,,#
TIMER,20,60#
```

For full device configuration, see [docs/usage/CONFIGURATION_GUIDE.md](docs/usage/CONFIGURATION_GUIDE.md).  
For deployment details, see [server/DEPLOYMENT.md](server/DEPLOYMENT.md).

## 📡 API Endpoints

Full documentation with request/response shapes: **[server/API_ENDPOINTS.md](server/API_ENDPOINTS.md)**  
Interactive docs: **https://api.track-iq.tech/docs**

### Auth
- `POST /api/auth/sync` — Sync Clerk user (no auth needed)
- `GET /api/auth/me` — Current user profile
- `GET /api/auth/user/{clerk_id}` — User by Clerk ID
- `POST /api/auth/admin-create-user` — Create user (admin)

### Onboarding
- `POST /api/users` — Create/update user profile
- `POST /api/devices/pair` — Pair device by IMEI + PIN
- `GET /api/devices/{imei}/status` — Poll device status (by IMEI)
- `POST /api/vehicles` — Register vehicle
- `GET /api/vehicles` — List user's vehicles
- `POST /api/payments/verify` — Verify Flutterwave payment
- `POST /api/subscriptions` — Activate subscription
- `POST /api/subscriptions/upgrade` — Upgrade plan
- `GET /api/billing` — Current plan + payment history

### Device Management
- `GET /api/devices/` — List devices
- `GET /api/devices/{id}` — Get device by ID
- `GET /api/devices/imei/{imei}` — Get device by IMEI
- `POST /api/devices/` — Register device (admin)
- `PUT /api/devices/{id}` — Update device
- `DELETE /api/devices/{id}` — Delete device
- `GET /api/devices/{id}/status` — Device status + battery
- `GET /api/devices/{id}/diagnostics` — Packet interval analysis
- `GET /api/devices/{id}/trips` — List device trips

### Remote Commands (sent over TCP, no SMS)
- `POST /api/devices/{id}/command` — Send raw command
- `POST /api/devices/{id}/alarm/vibration` — Toggle vibration alarm
- `POST /api/devices/{id}/alarm/lowbattery` — Toggle low battery alarm
- `POST /api/devices/{id}/alarm/acc` — Toggle ACC alarm
- `POST /api/devices/{id}/alarm/overspeed` — Toggle overspeed alarm
- `POST /api/devices/{id}/alarm/displacement` — Toggle movement alarm
- `POST /api/devices/{id}/alarm/sos` — Toggle SOS alarm
- `POST /api/devices/{id}/fuel/cut` — Immobilize vehicle
- `POST /api/devices/{id}/fuel/restore` — Re-enable vehicle
- `POST /api/devices/{id}/query/location` — Request location from device
- `POST /api/devices/{id}/query/status` — Request status from device

### Location Tracking
- `GET /api/locations/{device_id}/latest` — Latest position
- `GET /api/locations/{device_id}/history` — Location history
- `GET /api/locations/{device_id}/route` — Route as GeoJSON FeatureCollection
- `GET /api/locations/{device_id}/route-line` — Route as GeoJSON LineString
- `GET /api/locations/{device_id}/distance` — Total distance in time range
- `GET /api/locations/{device_id}/alarms` — Alarm events
- `GET /api/locations/nearby` — Find nearby devices

### Trips
- `GET /api/trips` — List trips for device
- `POST /api/trips` — Create trip from history
- `POST /api/trips/start` — Start active trip
- `GET /api/trips/suggested` — Auto-suggested trip segments
- `GET /api/trips/settings` — Trip segmentation settings
- `PUT /api/trips/settings` — Update trip settings
- `GET /api/trips/{id}` — Trip detail + route
- `POST /api/trips/{id}/end` — End active trip
- `DELETE /api/trips/{id}` — Delete trip

### Real-time
- `WS /ws/locations/{device_id}` — Live location stream

## 🔧 Device Configuration Tools

### Monitor Device Status
```bash
sudo ./device_info.py
```
Shows: Battery level, GPS status, GSM signal, server connection, device info

### Configure Device
```bash
sudo ./gps_config.py
```
Interactive tool to configure server IP, port, and APN settings

### Test Connection
```bash
sudo ./test_connection.py
```
Auto-detects baud rate and tests device communication

## 🏗️ Technology Stack

- **Backend:** FastAPI 0.109.0 + Python 3.11
- **Database:** PostgreSQL 15 + PostGIS 3.3
- **ORM:** SQLAlchemy 2.0.25 + GeoAlchemy2
- **Async:** asyncio for TCP server
- **Deployment:** Docker + Docker Compose
- **API Docs:** OpenAPI (Swagger UI)

## 📚 Documentation

- [Server README](server/README.md) - Complete server documentation
- [Deployment Guide](server/DEPLOYMENT.md) - Production deployment
- [Local Setup](server/LOCAL_SETUP.md) - Local testing guide
- [Quick Reference](server/QUICK_REFERENCE.md) - Common commands
- [Architecture](Architecture.md) - System design

## 🧪 Testing

### Check Server Health
```bash
curl http://localhost:8000/health
```

### List Devices
```bash
curl http://localhost:8000/api/devices/ | jq
```

### Get Latest Location
```bash
curl http://localhost:8000/api/locations/1/latest | jq
```

### Watch Logs
```bash
cd server
docker compose logs -f gps_server
```

## 🛠️ Development

### Setup Development Environment

```bash
cd server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Run Without Docker

```bash
# Install PostgreSQL + PostGIS
sudo apt install postgresql-15 postgresql-15-postgis-3

# Create database
sudo -u postgres psql -c "CREATE DATABASE gps_tracking;"
sudo -u postgres psql -c "CREATE USER gps_user WITH PASSWORD 'password';"
sudo -u postgres psql -c "GRANT ALL ON DATABASE gps_tracking TO gps_user;"

# Run server
python -m app.main
```

## 🔐 Security Considerations

- Change default `SECRET_KEY` in `.env`
- Use strong database passwords
- Enable HTTPS with nginx reverse proxy
- Implement JWT authentication (User model ready)
- Configure firewall to restrict access
- Regular security updates

## 📊 Hardware Compatibility

**Supported Devices:**

| # | Model | Network | Config Method |
|---|-------|---------|---------------|
| 1 | TK903ELE (v1.1.6) | GSM/GPRS | SMS `SERVER,0,...` / USB AT commands |
| 2 | G900LS J16-4G | LTE/GSM | SMS `SERVER,1,...` |

**Protocol:** GT06 binary (`0x7878...0x0D0A`)

**Packet Types Supported:**
- `0x01` — Login (IMEI authentication)
- `0x12` — Location data (GPS coordinates)
- `0x13` — Heartbeat (battery, signal)
- `0x15` — Command response
- `0x16` — Alarm events (SOS, vibration, etc.)
- `0x80` — Server→Device commands

**SIM:** Any SIM with GPRS/LTE data (MTN Rwanda: APN `internet`)

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📝 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 👤 Author

**Leo**
- Project: Track IQ GPS Tracking System
- Server: api.track-iq.tech

## 🆘 Support

- **Documentation:** Check docs/ folder and server/README.md
- **Issues:** Open a GitHub issue
- **API Docs:** http://your-server:8000/docs

## 🎯 Roadmap

**Completed ✅**
- [x] Clerk JWT authentication
- [x] WebSocket real-time location streaming
- [x] Alarm events (SOS, vibration, overspeed, ACC, power cut)
- [x] Admin web dashboard
- [x] Multi-user support
- [x] Remote command sending over TCP
- [x] Trip management with auto-detection
- [x] Subscription / billing (Flutterwave)
- [x] Distance calculation, route playback
- [x] Geofence model (DB schema ready)

**Planned 📋**
- [ ] Mobile app (Flutter / React Native)
- [ ] Geofence alerts (active enforcement)
- [ ] Driver behavior analysis (harsh braking, acceleration)
- [ ] Fuel consumption tracking
- [ ] OTA firmware updates
- [ ] Multi-tenant fleet management

## 📞 GPS Tracker Commands (SMS)

**TK903ELE:**
```
SERVER,0,api.track-iq.tech,7018,0#
APN,internet,,#
fix020s060m***n123456
check123456
```

**G900LS J16-4G:**
```
SERVER,1,api.track-iq.tech,7018,0#
APN,internet,,#
TIMER,20,60#
SZCS#SLPDISCONNECT=0
RESET#
```

Full command reference: [docs/usage/CONFIGURATION_GUIDE.md](docs/usage/CONFIGURATION_GUIDE.md)

## 🌐 Useful Links

- [TK903ELE Documentation](docs/)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [PostGIS Documentation](https://postgis.net/)
- [Docker Documentation](https://docs.docker.com/)

---

**Happy Tracking! 🚗📍🗺️**

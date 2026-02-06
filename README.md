# GPS Tracking Application

A complete GPS tracking solution for TK903ELE GPS trackers with real-time monitoring, REST API, and location history.

## 🚀 Features

- **Real-time GPS tracking** - Track multiple devices simultaneously
- **TCP Server** - Receives binary protocol data on port 7018
- **REST API** - Complete API for device and location management
- **PostGIS Integration** - Spatial queries and GeoJSON export
- **Battery Monitoring** - Track device battery and signal strength
- **Location History** - Store and query historical GPS data
- **Docker Deployment** - One-command setup with Docker Compose
- **USB Configuration** - Configure devices via USB connection
- **🆕 Clerk Authentication** - Multi-user support with Clerk authentication integration

## 🔐 NEW: Clerk Authentication Integration

**Status:** ✅ **COMPLETE AND READY FOR DEPLOYMENT**

The backend now supports **Clerk authentication** for mobile apps with complete user management and access control.

### Key Features
- ✅ User sync endpoint for Clerk authentication
- ✅ Device ownership and filtering by user
- ✅ Access control on location data
- ✅ Header-based authentication (`X-Clerk-User-Id`)
- ✅ Backward compatible with existing system

### Quick Links
- **[Implementation Summary](CLERK_IMPLEMENTATION_SUMMARY.md)** - Overview and mobile integration
- **[Complete Documentation](server/CLERK_AUTH_IMPLEMENTATION.md)** - Full API specification
- **[Quick Setup Guide](server/QUICK_SETUP.md)** - Deployment instructions
- **[Deployment Checklist](DEPLOYMENT_CHECKLIST.md)** - Step-by-step deployment
- **[Architecture Diagrams](server/ARCHITECTURE_DIAGRAM.md)** - Visual flow diagrams
- **[Test Suite](server/test_clerk_auth.py)** - Automated tests

### New API Endpoints
```
POST /api/auth/sync              - Sync Clerk user to database
GET  /api/auth/user/{clerk_id}   - Get user by Clerk ID
POST /api/devices/{id}/assign    - Assign device to user
```

### Mobile App Integration
```javascript
// Sync user after Clerk sign-in
await fetch('http://164.92.212.186:8000/api/auth/sync', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    clerk_user_id: user.id,
    email: user.emailAddresses[0].emailAddress,
    name: user.fullName,
  }),
});

// Add header to all API requests
await fetch('http://164.92.212.186:8000/api/devices', {
  headers: { 'X-Clerk-User-Id': clerkUserId },
});
```

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
- Domain name (optional): api.gocavgo.com

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
curl http://localhost:8000/health
```

### Configure GPS Tracker for Production

**SMS Method:**
```sms
SERVER#your-server-ip#7018#
APN#internet#
```

**USB Method:**
```bash
sudo ./gps_config.py
# Enter production IP
```

For detailed deployment instructions, see [server/DEPLOYMENT.md](server/DEPLOYMENT.md)

## 📡 API Endpoints

### Device Management

- `GET /api/devices/` - List all devices
- `GET /api/devices/{id}` - Get device by ID
- `GET /api/devices/imei/{imei}` - Get device by IMEI
- `POST /api/devices/` - Create device
- `PUT /api/devices/{id}` - Update device
- `DELETE /api/devices/{id}` - Delete device
- `GET /api/devices/{id}/status` - Device status

### Location Tracking

- `GET /api/locations/{device_id}/latest` - Latest position
- `GET /api/locations/{device_id}/history` - Location history
- `GET /api/locations/{device_id}/route` - Route as GeoJSON
- `GET /api/locations/{device_id}/alarms` - Alarm events
- `GET /api/locations/nearby` - Find nearby devices

Full API documentation: http://your-server:8000/docs

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

**Tested with:**
- TK903ELE GPS Tracker (v1.1.6)
- Protocol: Binary 0x7878...0x0D0A
- Connection: USB via CH341 or TCP
- SIM: MTN Rwanda (IoT/M2M recommended)

**Packet Types Supported:**
- 0x01 - Login (IMEI authentication)
- 0x12 - Location data (GPS coordinates)
- 0x13 - Heartbeat (battery, signal)
- 0x16 - Alarm events (SOS, geofence)
- 0x80 - Command responses

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
- Project: GOCAVGO GPS Tracking System
- Server: api.gocavgo.com

## 🆘 Support

- **Documentation:** Check docs/ folder and server/README.md
- **Issues:** Open a GitHub issue
- **API Docs:** http://your-server:8000/docs

## 🎯 Roadmap

- [ ] JWT authentication
- [ ] WebSocket for real-time updates
- [ ] Geofencing with alerts
- [ ] Mobile app (Flutter)
- [ ] Web dashboard
- [ ] Multi-user support
- [ ] Command sending to devices
- [ ] Route playback
- [ ] Analytics and reports

## 📞 GPS Tracker Commands (SMS)

```sms
# Set server
SERVER#ip_address#port#

# Set APN
APN#internet#

# Query status
STATUS#

# Restart device
RESET#
```

## 🌐 Useful Links

- [TK903ELE Documentation](docs/)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [PostGIS Documentation](https://postgis.net/)
- [Docker Documentation](https://docs.docker.com/)

---

**Happy Tracking! 🚗📍🗺️**

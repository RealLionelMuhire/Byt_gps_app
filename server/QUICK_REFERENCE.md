# Quick Reference - GPS Tracking Server

## 🏠 Test Locally First!

```bash
cd /home/leo/BYThron/Byt_gps_app/server
sudo docker-compose up -d
curl http://localhost:8000/health

# Configure device to local IP
ip addr show | grep "inet "  # Find IP like 192.168.1.100
sudo ../gps_config.py         # Use 192.168.1.100:7018
```

**Full local testing guide:** [LOCAL_SETUP.md](LOCAL_SETUP.md)

---

## 🚀 Deploy to Production (After Testing)

```bash
# On server 164.92.212.186
cd /opt/gps-tracking-server
chmod +x setup.sh
sudo ./setup.sh
```

## 📡 Configure GPS Tracker

**SMS Commands:**
```sms
SERVER#164.92.212.186#7018#
APN#internet#
```

**Or USB:**
```bash
sudo ./gps_config.py
```

## 🔍 Check Status

**Server Health:**
```bash
curl http://164.92.212.186:8000/health
```

**Device Status:**
```bash
sudo ./device_info.py
```

## 📊 API Endpoints (Quick)

```bash
# List all devices
GET http://164.92.212.186:8000/api/devices/

# Latest location
GET http://164.92.212.186:8000/api/locations/{device_id}/latest

# Location history (last 24h)
GET http://164.92.212.186:8000/api/locations/{device_id}/history

# Device route (GeoJSON)
GET http://164.92.212.186:8000/api/locations/{device_id}/route
```

## 🐳 Docker Commands

```bash
# Start
docker-compose up -d

# Stop
docker-compose down

# Restart
docker-compose restart

# Logs
docker-compose logs -f

# Status
docker-compose ps
```

## 🔧 Troubleshooting

**Port not accessible:**
```bash
sudo ufw allow 7018/tcp
telnet 164.92.212.186 7018
```

**Check logs:**
```bash
docker-compose logs -f gps_server
```

**Restart everything:**
```bash
docker-compose down && docker-compose up -d
```

## 📱 Mobile App Integration

**Base URL:** `http://164.92.212.186:8000`

**Auth:** Not implemented yet (add JWT later)

**Key Endpoints:**
- `/api/devices/` - Device list
- `/api/locations/{id}/latest` - Current location
- `/api/locations/{id}/history` - Historical data

**Full Docs:** http://164.92.212.186:8000/docs

## 🎯 Your Setup

- **Server IP:** 164.92.212.186
- **Domain:** api.gocavgo.com
- **HTTP Port:** 8000
- **TCP Port:** 7018
- **Device IMEI:** 868720064874575

---

**Need help? Check README.md or DEPLOYMENT.md**

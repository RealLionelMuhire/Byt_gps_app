# 🏠 Local Development Setup

**Test your GPS tracking server locally before deploying to production!**

This guide helps you run and test the server on your Ubuntu 24.04 laptop.

---

## 📋 Prerequisites

- Ubuntu 24.04 (your current system)
- Python 3.11+
- PostgreSQL 15 with PostGIS
- Your GPS tracker (IMEI: 868720064874575)
- USB cable connected to /dev/ttyUSB0

---

## 🚀 Quick Start (Local)

### Option 1: Docker (Recommended)

```bash
cd /home/leo/BYThron/Byt_gps_app/server

# Install Docker if not already installed
sudo apt update
sudo apt install -y docker.io docker-compose

# Start services locally
sudo docker-compose up -d

# Check if running
sudo docker-compose ps
```

**Your local server is now running:**
- TCP Server: `localhost:7018`
- REST API: http://localhost:8000
- API Docs: http://localhost:8000/docs

### Option 2: Python Virtual Environment

```bash
cd /home/leo/BYThron/Byt_gps_app/server

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install and setup PostgreSQL
sudo apt install -y postgresql-15 postgresql-15-postgis-3

# Create database
sudo -u postgres psql << EOF
CREATE DATABASE gps_tracking;
CREATE USER gps_user WITH PASSWORD 'gps_password_local';
GRANT ALL PRIVILEGES ON DATABASE gps_tracking TO gps_user;
\c gps_tracking
CREATE EXTENSION postgis;
EOF

# Create .env file
cp .env.example .env
nano .env
```

Edit `.env`:
```env
APP_NAME="GPS Tracking Server - Local"
DEBUG=True
HOST=0.0.0.0
HTTP_PORT=8000
TCP_PORT=7018
DATABASE_URL=postgresql://gps_user:gps_password_local@localhost/gps_tracking
SECRET_KEY=local-dev-secret-key-change-in-production
CORS_ORIGINS=http://localhost:3000,http://localhost:8080
LOG_LEVEL=DEBUG
```

Initialize database:
```bash
# Run init script
sudo -u postgres psql gps_tracking < init_db.sql

# Create tables
python -c "from app.core.database import init_db; init_db()"
```

Start server:
```bash
python -m app.main
```

---

## 🧪 Testing Locally

### 1. Check Server Health

Open browser or terminal:
```bash
curl http://localhost:8000/health
```

Expected response:
```json
{
  "status": "healthy",
  "tcp_connections": 0,
  "active_devices": 0
}
```

### 2. Check API Documentation

Open in browser: http://localhost:8000/docs

You should see interactive Swagger UI with all API endpoints.

### 3. Test TCP Port

```bash
telnet localhost 7018
```

Should connect successfully. Press Ctrl+] then type `quit`.

### 4. Configure GPS Tracker to Use Local Server

**Important:** Configure your tracker to point to your local IP, not production!

Find your local IP:
```bash
ip addr show | grep "inet " | grep -v 127.0.0.1
```

Example output: `192.168.1.100`

**Option A: USB Configuration**

```bash
cd /home/leo/BYThron/Byt_gps_app

# First check current config
sudo ./device_info.py

# Configure to local server
sudo ./gps_config.py
# Enter your local IP: 192.168.1.100
# Port: 7018
# APN: internet
```

**Option B: SMS Configuration**

Send to your device SIM number:
```sms
SERVER#192.168.1.100#7018#
```

⚠️ **Note:** Your laptop and GPS tracker need to be on same network for this to work, OR use your laptop's public IP if the tracker has internet via mobile data.

### 5. Test with Real GPS Tracker

```bash
# Terminal 1: Watch server logs
cd /home/leo/BYThron/Byt_gps_app/server
sudo docker-compose logs -f gps_server
# OR if running Python directly:
# Just watch the output

# Terminal 2: Monitor device
cd /home/leo/BYThron/Byt_gps_app
sudo ./device_info.py

# Terminal 3: Check API
# Wait 30 seconds for device to send data, then:
curl http://localhost:8000/api/devices/
curl http://localhost:8000/api/devices/imei/868720064874575
```

### 6. Simulate GPS Data (Optional Testing)

If you want to test without the real device:

```bash
# Create test script
cat > test_gps_client.py << 'EOF'
#!/usr/bin/env python3
import socket
import struct
import time

def create_login_packet(imei):
    """Create login packet"""
    imei_bytes = imei.encode('ascii')
    length = len(imei_bytes)
    
    packet = struct.pack('>H', 0x7878)  # Start bit
    packet += struct.pack('B', length + 5)  # Length
    packet += struct.pack('B', 0x01)  # Protocol: Login
    packet += imei_bytes
    packet += struct.pack('>H', 0x0001)  # Serial
    
    # Calculate CRC (simplified)
    crc = 0
    for b in packet[2:-2]:
        crc ^= b
    packet += struct.pack('>H', crc)
    packet += struct.pack('>H', 0x0D0A)  # Stop bit
    return packet

def create_location_packet():
    """Create location packet with test coordinates"""
    packet = struct.pack('>H', 0x7878)  # Start bit
    packet += struct.pack('B', 35)  # Length
    packet += struct.pack('B', 0x12)  # Protocol: Location
    
    # DateTime (2026-01-22 14:30:00)
    packet += struct.pack('BBBBBB', 26, 1, 22, 14, 30, 0)
    
    # GPS: 12 satellites, valid
    packet += struct.pack('BB', 12, 0xF0)
    
    # Latitude: -1.95 (Kigali, Rwanda)
    lat = int((-1.95) * 30000 / 0.000001)
    packet += struct.pack('>I', lat & 0xFFFFFFFF)
    
    # Longitude: 30.06 (Kigali, Rwanda)
    lon = int(30.06 * 30000 / 0.000001)
    packet += struct.pack('>I', lon & 0xFFFFFFFF)
    
    # Speed: 45 km/h
    packet += struct.pack('B', 45)
    
    # Course: 180 degrees, Status
    packet += struct.pack('>H', (180 << 6) | 0x02)
    
    # MCC, MNC, LAC, Cell ID
    packet += struct.pack('>HHHI', 635, 10, 1234, 5678)
    
    packet += struct.pack('>H', 0x0001)  # Serial
    
    # CRC (simplified)
    crc = 0
    for b in packet[2:]:
        crc ^= b
    packet += struct.pack('>H', crc)
    packet += struct.pack('>H', 0x0D0A)  # Stop bit
    return packet

# Connect and send
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.connect(('localhost', 7018))

print("Sending login...")
sock.send(create_login_packet('868720064874575'))
response = sock.recv(1024)
print(f"Login response: {response.hex()}")

time.sleep(2)

print("Sending location...")
sock.send(create_location_packet())
response = sock.recv(1024)
print(f"Location response: {response.hex()}")

sock.close()
print("Done!")
EOF

chmod +x test_gps_client.py
python3 test_gps_client.py
```

Check if data appeared:
```bash
curl http://localhost:8000/api/devices/
curl http://localhost:8000/api/devices/imei/868720064874575
curl http://localhost:8000/api/locations/1/latest
```

---

## 🔍 Verification Checklist

Before deploying to production, verify:

- [ ] **Server starts without errors**
  ```bash
  sudo docker-compose ps
  # OR check Python output
  ```

- [ ] **Database connected**
  ```bash
  sudo docker-compose exec postgres psql -U gps_user -d gps_tracking -c "SELECT postgis_version();"
  ```

- [ ] **Health endpoint responds**
  ```bash
  curl http://localhost:8000/health
  ```

- [ ] **API documentation loads**
  - Visit http://localhost:8000/docs

- [ ] **TCP port accepts connections**
  ```bash
  telnet localhost 7018
  ```

- [ ] **GPS tracker connects**
  - Configure device to point to local IP
  - Check logs for login packet
  - Verify device appears in: `curl http://localhost:8000/api/devices/`

- [ ] **Location data saves**
  - Place device outdoors for GPS lock
  - Wait 30-60 seconds
  - Check: `curl http://localhost:8000/api/locations/1/latest`

- [ ] **GeoJSON export works**
  ```bash
  curl http://localhost:8000/api/locations/1/route
  # Should return valid GeoJSON
  ```

---

## 🐛 Local Troubleshooting

### Port Already in Use

```bash
# Check what's using the port
sudo ss -tulpn | grep :7018
sudo ss -tulpn | grep :8000

# Kill the process or change ports in .env
```

### PostgreSQL Connection Error

```bash
# Check if PostgreSQL is running
sudo systemctl status postgresql

# Check if database exists
sudo -u postgres psql -l | grep gps_tracking

# Check user permissions
sudo -u postgres psql -c "\du"
```

### Device Won't Connect

1. **Check firewall:**
   ```bash
   sudo ufw status
   sudo ufw allow 7018/tcp
   sudo ufw allow 8000/tcp
   ```

2. **Check local IP:**
   ```bash
   ip addr show
   # Make sure device is configured with correct IP
   ```

3. **Check device config:**
   ```bash
   sudo ./device_info.py
   # Should show: Server 192.168.1.x:7018
   ```

4. **Watch logs in real-time:**
   ```bash
   sudo docker-compose logs -f gps_server
   # Look for connection attempts
   ```

### No GPS Data

1. **Device needs GPS lock:**
   - Take device outdoors
   - Wait 2-5 minutes for GPS lock
   - Check: `sudo ./device_info.py` - GPS should show valid coordinates

2. **Check device sends location packets:**
   - Device sends data every 10-30 seconds when moving
   - Or every 5 minutes when stationary

---

## 📊 View Your Data

### Check Database Directly

```bash
# Docker
sudo docker-compose exec postgres psql -U gps_user -d gps_tracking

# Local PostgreSQL
sudo -u postgres psql gps_tracking
```

SQL queries:
```sql
-- List devices
SELECT id, imei, name, status, battery_level, gsm_signal, last_update 
FROM devices;

-- List locations
SELECT device_id, latitude, longitude, speed, gps_valid, timestamp 
FROM locations 
ORDER BY timestamp DESC 
LIMIT 10;

-- Count locations per device
SELECT device_id, COUNT(*) as location_count 
FROM locations 
GROUP BY device_id;

-- Check PostGIS geometry
SELECT device_id, ST_AsText(geom) as location, timestamp 
FROM locations 
WHERE geom IS NOT NULL 
ORDER BY timestamp DESC 
LIMIT 5;
```

### Use API Endpoints

```bash
# List all devices
curl http://localhost:8000/api/devices/ | jq

# Get specific device
curl http://localhost:8000/api/devices/1 | jq

# Latest location
curl http://localhost:8000/api/locations/1/latest | jq

# Location history (last 24h)
curl http://localhost:8000/api/locations/1/history | jq

# Route as GeoJSON
curl http://localhost:8000/api/locations/1/route | jq

# Find devices near coordinates
curl "http://localhost:8000/api/locations/nearby?latitude=-1.95&longitude=30.06&radius_km=10" | jq
```

---

## 🎯 Testing Scenarios

### Scenario 1: Device Powers On
1. Power on GPS tracker
2. Watch logs: `sudo docker-compose logs -f gps_server`
3. Should see: "Login received from device"
4. Check: `curl http://localhost:8000/api/devices/`

### Scenario 2: Device Sends Location
1. Take device outdoors
2. Wait for GPS lock (2-5 minutes)
3. Device sends location every 10-30 seconds
4. Check: `curl http://localhost:8000/api/locations/1/latest`

### Scenario 3: Battery Monitoring
1. Device sends heartbeat every 5 minutes
2. Check: `curl http://localhost:8000/api/devices/1/status`
3. Should show battery_level and gsm_signal

### Scenario 4: Alarm Event
1. Press SOS button on device (if available)
2. Watch logs for alarm packet
3. Check: `curl http://localhost:8000/api/locations/1/alarms`

---

## 🚀 Ready for Production?

Once everything works locally:

1. **Reset device to default:**
   ```bash
   # Configure device back to production server
   sudo ./gps_config.py
   # Enter: 164.92.212.186, port 7018
   ```

2. **Follow deployment guide:**
   - See [DEPLOYMENT.md](DEPLOYMENT.md)
   - Deploy to 164.92.212.186 (api.gocavgo.com)

3. **Configure for production:**
   - Use strong SECRET_KEY
   - Set DEBUG=False
   - Configure proper CORS_ORIGINS
   - Set up HTTPS with nginx

---

## 💡 Development Tips

### Hot Reload

When running with Python directly (not Docker), the server auto-reloads when you edit files.

### Debug Mode

Set `LOG_LEVEL=DEBUG` in `.env` to see detailed logs:
```env
LOG_LEVEL=DEBUG
```

### Test Multiple Devices

If you have multiple trackers, they'll all connect to same server. Each gets its own device ID based on IMEI.

### Database Reset

To start fresh:
```bash
# Docker
sudo docker-compose down -v
sudo docker-compose up -d

# Local
sudo -u postgres psql << EOF
DROP DATABASE gps_tracking;
CREATE DATABASE gps_tracking;
GRANT ALL PRIVILEGES ON DATABASE gps_tracking TO gps_user;
\c gps_tracking
CREATE EXTENSION postgis;
EOF

# Reinitialize
sudo -u postgres psql gps_tracking < init_db.sql
python -c "from app.core.database import init_db; init_db()"
```

### Watch Everything

```bash
# Terminal 1: Server logs
sudo docker-compose logs -f gps_server

# Terminal 2: Device monitor
watch -n 2 'curl -s http://localhost:8000/api/devices/ | jq'

# Terminal 3: Latest location
watch -n 5 'curl -s http://localhost:8000/api/locations/1/latest | jq'

# Terminal 4: Device USB info
sudo ./device_info.py
```

---

## 📚 Next Steps

After local testing:
1. ✅ Verify all features work
2. ✅ Test with real GPS tracker
3. ✅ Check location data accuracy
4. 🚀 Deploy to production: [DEPLOYMENT.md](DEPLOYMENT.md)
5. 📱 Start mobile app development
6. 🔒 Add authentication and HTTPS

---

**Happy Local Testing! 🏠🧪**

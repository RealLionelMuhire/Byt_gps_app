# Deployment Guide - 164.92.212.186 (api.gocavgo.com)

## 🎯 Quick Deployment (Recommended)

### 1. Upload Files to Server

```bash
# From your local machine
cd /home/leo/BYThron/Byt_gps_app
scp -r server/ root@164.92.212.186:/opt/gps-tracking-server/
```

### 2. SSH to Server

```bash
ssh root@164.92.212.186
cd /opt/gps-tracking-server
```

### 3. Run Setup Script

```bash
chmod +x setup.sh
sudo ./setup.sh
```

**Done!** Server will be running on:
- **HTTP API:** http://164.92.212.186:8000
- **TCP Port:** 164.92.212.186:7018

---

## 📋 Manual Deployment Steps

If you prefer step-by-step:

### Step 1: Install Docker

```bash
ssh root@164.92.212.186

# Install Docker
sudo apt update
sudo apt install -y docker.io docker-compose
sudo systemctl start docker
sudo systemctl enable docker
```

### Step 2: Upload Project

```bash
# Create directory
sudo mkdir -p /opt/gps-tracking-server
cd /opt/gps-tracking-server

# Upload files (from your local machine)
scp -r server/* root@164.92.212.186:/opt/gps-tracking-server/
```

### Step 3: Configure Environment

```bash
cd /opt/gps-tracking-server

# Create .env file
cp .env.example .env

# Generate secret key
SECRET_KEY=$(openssl rand -hex 32)

# Edit .env
nano .env
# Set SECRET_KEY, DATABASE_URL, etc.
```

### Step 4: Configure Firewall

```bash
# Allow HTTP API
sudo ufw allow 8000/tcp

# Allow TCP for GPS trackers
sudo ufw allow 7018/tcp

# Enable firewall
sudo ufw enable

# Check status
sudo ufw status
```

### Step 5: Start Services

```bash
# Start with Docker Compose
docker-compose up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f
```

### Step 6: Verify Deployment

```bash
# Test HTTP API
curl http://164.92.212.186:8000/health

# Test TCP port
telnet 164.92.212.186 7018

# View API docs
open http://164.92.212.186:8000/docs
```

---

## 🔧 Configure GPS Tracker

### Option 1: SMS Configuration

Send SMS to your GPS tracker's SIM number:

```sms
PASSWORD#123456#
APN#internet#
SERVER#164.92.212.186#7018#
```

Or use domain:
```sms
SERVER#api.gocavgo.com#7018#
```

### Option 2: USB Configuration

```bash
# On your local machine with device connected
cd /home/leo/BYThron/Byt_gps_app
sudo ./gps_config.py

# Enter:
# Server: 164.92.212.186 (or api.gocavgo.com)
# Port: 7018
# APN: internet
```

---

## ✅ Verification Checklist

After deployment, verify:

- [ ] HTTP API accessible: `curl http://164.92.212.186:8000/health`
- [ ] TCP port open: `telnet 164.92.212.186 7018`
- [ ] API docs available: http://164.92.212.186:8000/docs
- [ ] Database running: `docker-compose ps`
- [ ] Firewall configured: `sudo ufw status`
- [ ] GPS tracker configured with server IP
- [ ] Device status visible: `sudo ./device_info.py`

---

## 📊 Monitoring

### Check Server Status

```bash
# Docker containers
docker-compose ps

# View logs
docker-compose logs -f gps_server

# Check connections
curl http://164.92.212.186:8000/health
```

### View Connected Devices

```bash
# API call
curl http://164.92.212.186:8000/api/devices/
```

---

## 🐛 Troubleshooting

### Port 7018 Not Accessible

```bash
# Check firewall
sudo ufw status
sudo ufw allow 7018/tcp

# Check if service is listening
sudo netstat -tlnp | grep 7018

# Check Docker logs
docker-compose logs gps_server
```

### Database Connection Error

```bash
# Check PostgreSQL container
docker-compose ps postgres

# View database logs
docker-compose logs postgres

# Restart database
docker-compose restart postgres
```

### GPS Tracker Not Connecting

1. **Verify server is accessible:**
   ```bash
   telnet 164.92.212.186 7018
   ```

2. **Check device configuration:**
   ```bash
   sudo ./device_info.py
   # Should show: Server: 164.92.212.186:7018
   ```

3. **Check server logs:**
   ```bash
   docker-compose logs -f gps_server
   # Look for connection attempts
   ```

---

## 🔄 Updates and Maintenance

### Update Application Code

```bash
cd /opt/gps-tracking-server

# Pull latest changes
git pull  # if using git

# Or upload new files
scp -r server/app/* root@164.92.212.186:/opt/gps-tracking-server/app/

# Restart services
docker-compose restart gps_server
```

### Backup Database

```bash
# Export database
docker-compose exec postgres pg_dump -U gps_user gps_tracking > backup_$(date +%Y%m%d).sql

# Or use Docker volume backup
docker run --rm -v gps-tracking-server_postgres_data:/data -v $(pwd):/backup ubuntu tar czf /backup/postgres_backup.tar.gz /data
```

### View Logs

```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f gps_server

# Last 100 lines
docker-compose logs --tail=100 gps_server
```

---

## 🚀 Production Recommendations

### 1. Add HTTPS with Nginx

```bash
sudo apt install nginx certbot python3-certbot-nginx

# Configure Nginx
sudo nano /etc/nginx/sites-available/gps-api

# Add SSL with Let's Encrypt
sudo certbot --nginx -d api.gocavgo.com
```

### 2. Set Up Monitoring

```bash
# Install Prometheus + Grafana
docker-compose -f docker-compose.monitoring.yml up -d
```

### 3. Database Backups

```bash
# Add to crontab
0 2 * * * cd /opt/gps-tracking-server && docker-compose exec postgres pg_dump -U gps_user gps_tracking > /backups/gps_$(date +\%Y\%m\%d).sql
```

### 4. Log Rotation

```bash
# Configure Docker log rotation
sudo nano /etc/docker/daemon.json

{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
```

---

## 📱 Mobile App Integration

Your mobile apps (Android/iOS) should connect to:

**Base URL:** `http://164.92.212.186:8000` or `https://api.gocavgo.com`

**Endpoints:**
- `GET /api/devices/` - List devices
- `GET /api/devices/{id}/status` - Device status
- `GET /api/locations/{device_id}/latest` - Latest location
- `GET /api/locations/{device_id}/history` - Location history
- `GET /api/locations/{device_id}/route` - Route (GeoJSON for maps)

**API Documentation:** http://164.92.212.186:8000/docs

---

## 🎉 Next Steps

1. **Test with your GPS tracker:**
   - Configure device with server IP
   - Place outdoors for GPS lock
   - Check if data appears in API

2. **Develop mobile apps:**
   - Use API documentation at /docs
   - Implement real-time tracking
   - Add maps (Google Maps, Mapbox)

3. **Scale as needed:**
   - Add more GPS trackers
   - Monitor performance
   - Upgrade resources if needed

---

**Your GPS tracking server is ready! 🎯📍**

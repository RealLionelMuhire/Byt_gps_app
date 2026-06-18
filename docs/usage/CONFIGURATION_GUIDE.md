# GPS Tracker — Configuration Guide

**Server:** `api.track-iq.tech`  
**TCP Port (GPS devices):** `7018`  
**HTTP API:** `https://api.track-iq.tech`

> ⚠️ **Important:** GPS devices connect via **raw TCP on port 7018** — they do NOT use HTTPS.  
> HTTPS is only for the mobile app and REST API. Do not include `https://` in device SMS commands.

---

## Supported Devices

| # | Model | Protocol | Config Method |
|---|-------|----------|---------------|
| 1 | **TK903ELE** | GT06 binary (`0x7878`) | SMS (`AT+ZDR=` / `SERVER,` style) |
| 2 | **G900LS J16-4G** | GT06 binary (`0x7878`) | SMS (`SERVER,` / `TIMER,` style) |

Both devices share the same binary protocol and the same TCP port. The only difference is the **SMS command syntax** used to configure them.

---
---

# Device 1 — TK903ELE

**Type:** Vehicle GPS tracker  
**Network:** GSM/GPRS (2G/3G)  
**SIM:** MTN Rwanda  
**Firmware:** v1.1.6  
**Default Password:** `123456`  

---

## Quick Setup (TK903ELE)

Send these SMS messages **in order** to the SIM number inside the device:

```
SERVER,0,api.track-iq.tech,7018,0#
APN,internet,,#
fix020s060m***n123456
reset123456
```

Verify with:
```
check123456
IPAPN123456
```

---

## Full Command Reference (TK903ELE)

### Server & Network

| Task | SMS Command | Expected Reply |
|------|-------------|----------------|
| Set server (IP mode) | `SERVER,0,api.track-iq.tech,7018,0#` | `server ok` |
| Set server (domain mode) | `SERVER,1,api.track-iq.tech,7018,0#` | `server ok` |
| Set APN (MTN Rwanda) | `APN,internet,,#` | `apn ok` |
| Set APN with credentials | `apn123456 internet username password` | — |
| Check IP & APN | `IPAPN123456` | Current server + APN |
| Check all settings | `check123456` | Full config dump |

### Upload Intervals

| Command | Moving | Parked |
|---------|--------|--------|
| `fix020s060m***n123456` | 20 s | 60 s (recommended) |
| `fix010s030m***n123456` | 10 s | 30 s |
| `fix030s120m***n123456` | 30 s | 120 s |

### Status & Information

| Task | SMS Command |
|------|-------------|
| Device status | `STATUS#` |
| Check all settings | `check123456` |
| Check IP/APN | `IPAPN123456` |
| Device version | `VERSION#` |

### Admin & Security

| Task | SMS Command |
|------|-------------|
| Set admin phone | `admin123456 +250XXXXXXXXX` |
| Remove admin phone | `noadmin123456 +250XXXXXXXXX` |
| Change password | `password123456 NEWPASSWORD` |
| Reset device | `reset123456` |
| Factory reset (⚠️ erases all) | `begin123456` |

### Alarms

| Task | SMS Command |
|------|-------------|
| Enable ACC on/off alarm | `acc123456` |
| Disable ACC alarm | `noacc123456` |
| Movement alarm (200m) | `move123456 0200` |
| Cancel movement alarm | `nomove123456` |
| Enable vibration alarm | `vibrate123456 1` |
| Disable vibration alarm | `vibrate123456 0` |
| Enable low battery alarm | `lowbattery123456 on` |
| Disable low battery alarm | `lowbattery123456 off` |

### Fuel/Relay Control

| Task | SMS Command |
|------|-------------|
| Cut fuel (immobilize) | `stop123456` |
| Resume fuel | `resume123456` |

> ⚠️ Only works if device is wired with a relay to the vehicle.

### Other Settings

| Task | SMS Command |
|------|-------------|
| Set sleep mode ON | `sleep123456 on` |
| Set sleep mode OFF | `sleep123456 off` |
| Set timezone (UTC+2) | `time zone123456 2` |
| Angle upload trigger | `ANGLE123456 30` |

---

## USB Monitoring (TK903ELE)

Connect device via USB (`/dev/ttyUSB0`) to monitor live data:

```bash
sudo ./device_info.py
sudo ./device_info.py --duration 60
sudo ./device_info.py --debug
```

Expected output:
```
🔋 Battery: 4.22V — 100%
🛰️  GPS:  LOCKED
📍 Location: -1.94XXXX°S, 30.05XXXX°E
📶 GSM Signal: CSQ: 22 (Strong)
🌐 Server: api.track-iq.tech:7018
```

---

## Real-World Setup Example (TK903ELE)

**Device Info:**
- IMEI: `868720064874575`
- SIM: MTN Rwanda

**1. Check current status:**
```sms
STATUS#
```
Reply: `V:4.22V,12V;CSQ:22;PWR:1;ACC:0;GPS:3,0,0;GS:0,1`

**2. Configure server:**
```sms
SERVER,0,api.track-iq.tech,7018,0#
```
Reply: `server ok`

**3. Set APN:**
```sms
APN,internet,,#
```
Reply: `apn ok`

**4. Set upload interval:**
```sms
fix020s060m***n123456
```

**5. Verify and restart:**
```sms
check123456
reset123456
```

**6. Confirm on server:**
```bash
curl https://api.track-iq.tech/api/devices/
curl https://api.track-iq.tech/api/locations/1/latest
```

---
---

# Device 2 — G900LS J16-4G

**Type:** Vehicle GPS tracker (4G LTE)  
**Network:** LTE-FDD/TDD + GSM fallback  
**Protocol:** GT06 series  
**Default Password:** `123456`  

---

## Quick Setup (G900LS J16-4G)

Send these SMS messages **in order** to the SIM number inside the device:

```
SERVER,1,api.track-iq.tech,7018,0#
APN,internet,,#
TIMER,20,60#
SZCS#SLPDISCONNECT=0
RESET#
```

Verify with:
```
PARAM#
STATUS#
```

---

## Full Command Reference (G900LS J16-4G)

### Server & Network

| Task | SMS Command | Expected Reply |
|------|-------------|----------------|
| Set server (domain mode) | `SERVER,1,api.track-iq.tech,7018,0#` | `server ok` |
| Set server (IP mode) | `SERVER,0,IP_ADDRESS,7018,0#` | `server ok` |
| Set APN (MTN Rwanda, no creds) | `APN,internet,,#` | `apn ok` |
| Set APN with credentials | `APN,internet,username,password#` | `apn ok` |
| Query APN | `APN#` | Current APN |
| Query server | `SERVER#` | Current server |
| Query all parameters | `PARAM#` | Full config dump |

### Upload Intervals (TIMER)

```
TIMER,T1,T2#
```
- `T1` = ACC **ON** (driving) interval: `5–60` seconds  
- `T2` = ACC **OFF** (parked) interval: `5–1800` seconds

| Command | Moving | Parked |
|---------|--------|--------|
| `TIMER,20,60#` | 20 s | 60 s (recommended) |
| `TIMER,10,300#` | 10 s | 5 min |
| `TIMER,30,1800#` | 30 s | 30 min (battery saving) |

Query: `TIMER#`

### Status & Information

| Task | SMS Command |
|------|-------------|
| Full status | `STATUS#` |
| All parameters | `PARAM#` |
| Firmware version | `VERSION#` |
| Device IMEI | `IMEI#` |

### Location Queries (SMS-based)

| Task | SMS Command | Returns |
|------|-------------|---------|
| GPS coordinates | `WHERE#` | Latitude, longitude |
| Full position | `POSITION#` | Full GPS info |
| Google Maps link | `URL#` | Clickable map URL |
| Address (local) | `123` | Street address |
| Address (international) | `666#` | Street address |

### Admin Phone Numbers (SOS / Alert)

| Task | SMS Command |
|------|-------------|
| Set center number 1 | `CENTER,A,+250XXXXXXXXX#` |
| Delete center number 1 | `CENTER,D#` |
| Set center number 2 | `CENTER,A2,+250XXXXXXXXX#` |
| Delete center number 2 | `CENTER,D2#` |
| Set center number 3 | `CENTER,A3,+250XXXXXXXXX#` |
| Query all center numbers | `CENTER#` |

### Alarms

#### Vibration Alarm
| Task | Command |
|------|---------|
| Enable (GPRS only) | `SENALM,ON,0#` |
| Enable (SMS + GPRS) | `SENALM,ON,1#` |
| Enable (GPRS + SMS + Call) | `SENALM,ON,2#` |
| Disable | `SENALM,OFF#` |
| Query | `SENALM#` |

#### Power Cut Alarm
| Task | Command |
|------|---------|
| Enable (GPRS, detect 5s, min charge 10s) | `POWERALM,ON,0,5,10,#` |
| Enable (SMS + GPRS) | `POWERALM,ON,1,5,10,#` |
| Disable | `POWERALM,OFF#` |
| Query | `POWERALM#` |

#### ACC Ignition ON Alarm
| Task | Command |
|------|---------|
| Enable (GPRS only) | `ACCALM,ON,0#` |
| Enable (GPRS + SMS) | `ACCALM,ON,1#` |
| Enable (GPRS + Call) | `ACCALM,ON,2#` |
| Enable (GPRS + SMS + Call) | `ACCALM,ON,3#` |
| Disable | `ACCALM,OFF#` |
| Query | `ACCALM#` |

#### ACC Ignition OFF Alarm
| Task | Command |
|------|---------|
| Enable (GPRS only) | `ACCOFFALM,ON,0#` |
| Enable (GPRS + SMS) | `ACCOFFALM,ON,1#` |
| Enable (GPRS + SMS + Call) | `ACCOFFALM,ON,3#` |
| Disable | `ACCOFFALM,OFF#` |
| Query | `ACCOFFALM#` |

### Fuel/Relay Control

| Task | Command |
|------|---------|
| Check relay status | `RELAY#` |
| **Cut fuel** (immobilize vehicle) | `RELAY,1#` |
| **Resume fuel** (re-enable vehicle) | `RELAY,0#` |

> ⚠️ `RELAY,1#` will cut the vehicle engine. Only use when the vehicle is safely stopped.

### Timezone (Rwanda = UTC+2)

| Task | Command |
|------|---------|
| Set SMS timezone (UTC+2) | `GMT,E,2,0#` |
| Query SMS timezone | `GMT#` |
| Set GPRS timezone | `SZCS#GT06GPRSGMT=2` |
| Query GPRS timezone | `CXCS#GT06GPRSGMT` |

### Sleep Mode

| Task | Command |
|------|---------|
| Enable sleep (disconnect TCP when parked) | `SZCS#SLPDISCONNECT=1` |
| Disable sleep (always connected) | `SZCS#SLPDISCONNECT=0` |
| Query sleep mode | `CXCS#SLPDISCONNECT` |

> 💡 Recommended: **`SZCS#SLPDISCONNECT=0`** — keeps TCP connection alive at all times.  
> If sleep is ON, the server marks the device offline when parked and it reconnects on ignition.

### Device Control

| Task | Command |
|------|---------|
| Restart device | `RESET#` |

---

## Real-World Setup Example (G900LS J16-4G)

**Before you start — collect from the device:**
1. **IMEI** — printed on the device label (15 digits), or send `IMEI#` via SMS
2. **SIM card** — inserted with an active data plan (not voice-only)
3. **APN** — confirm with your carrier (MTN Rwanda: `internet`)

**1. Set server:**
```sms
SERVER,1,api.track-iq.tech,7018,0#
```

**2. Set APN:**
```sms
APN,internet,,#
```

**3. Set upload interval:**
```sms
TIMER,20,60#
```

**4. Keep TCP alive (no sleep):**
```sms
SZCS#SLPDISCONNECT=0
```

**5. Set timezone (Rwanda UTC+2):**
```sms
GMT,E,2,0#
```

**6. Restart:**
```sms
RESET#
```

**7. Verify:**
```sms
PARAM#
STATUS#
```
The `PARAM#` reply should show `api.track-iq.tech` and port `7018`.

**8. Pre-register IMEI on server (required before connecting):**
```bash
# Via admin dashboard, or via API:
curl -X POST https://api.track-iq.tech/api/devices/ \
  -H "Content-Type: application/json" \
  -d '{"imei":"YOUR_15_DIGIT_IMEI","name":"G900LS-01"}'
```

**9. Monitor server logs:**
```bash
docker-compose logs -f | grep -i imei
```
Expected:
```
Device login attempt: IMEI XXXXXXXXXXXXXXX
Device accepted: IMEI XXXXXXXXXXXXXXX (id=2)
```

**10. Confirm tracking:**
```bash
curl https://api.track-iq.tech/api/devices/
curl https://api.track-iq.tech/api/locations/2/latest
```

---
---

# Shared — Server-Side Checklist (Both Devices)

Before any device can connect, it must be **pre-registered** in the database. Unknown IMEIs are rejected at handshake.

## Pre-Registration

```bash
# Register via API
curl -X POST https://api.track-iq.tech/api/devices/ \
  -H "Content-Type: application/json" \
  -d '{
    "imei": "YOUR_15_DIGIT_IMEI",
    "name": "Device Name",
    "description": "Optional notes"
  }'
```

## Firewall

```bash
sudo ufw allow 7018/tcp    # GPS device TCP port
sudo ufw allow 8000/tcp    # HTTP API (if not behind nginx)
```

## Health Check

```bash
curl https://api.track-iq.tech/health
```

## View All Devices

```bash
curl https://api.track-iq.tech/api/devices/
```

## View Latest Location

```bash
curl https://api.track-iq.tech/api/locations/{device_id}/latest
```

---

# Troubleshooting

## Device Not Connecting

| Check | How to verify |
|-------|---------------|
| Server address correct? | Send `PARAM#` or `check123456` |
| APN correct? | Send `APN#` or `IPAPN123456` |
| SIM has data plan? | Check with carrier |
| Port 7018 open? | `netstat -tuln \| grep 7018` on server |
| IMEI pre-registered? | `curl https://api.track-iq.tech/api/devices/` |
| Server running? | `curl https://api.track-iq.tech/health` |

## Server Log Patterns

| Log message | Meaning |
|-------------|---------|
| `Device login attempt: IMEI ...` | Device connected, checking DB |
| `Device accepted: IMEI ...` | ✅ Success |
| `Rejected unknown IMEI ...` | IMEI not in DB — pre-register it |
| `Invalid start bit` | ⚠️ May be 0x7979 extended frame (4G device) |
| `CRC mismatch` | Packet corruption — usually non-fatal |

## SMS Commands Not Working (TK903ELE)

1. Wrong password → try `123456`
2. Wrong command format → check G06L vs C32 format
3. SIM has no SMS balance
4. Wait 30s between commands

## SMS Commands Not Working (G900LS)

1. Commands require **no password** by default (no `123456` prefix)
2. Commands end with `#` — don't omit the hash
3. Ensure the SIM has SMS capability
4. `RESET#` after configuration changes

---

# Quick Reference Cards

## Device 1 — TK903ELE

| Task | Command |
|------|---------|
| Set server | `SERVER,0,api.track-iq.tech,7018,0#` |
| Set APN | `APN,internet,,#` |
| Upload interval | `fix020s060m***n123456` |
| Check status | `STATUS#` |
| Check settings | `check123456` |
| Reset | `reset123456` |

**TCP:** `api.track-iq.tech:7018`  
**APN:** `internet` (MTN Rwanda)  
**Password:** `123456`

---

## Device 2 — G900LS J16-4G

| Task | Command |
|------|---------|
| Set server | `SERVER,1,api.track-iq.tech,7018,0#` |
| Set APN | `APN,internet,,#` |
| Upload interval | `TIMER,20,60#` |
| Keep alive | `SZCS#SLPDISCONNECT=0` |
| Check status | `STATUS#` |
| Check all params | `PARAM#` |
| Fuel cut | `RELAY,1#` |
| Fuel resume | `RELAY,0#` |
| Reset | `RESET#` |

**TCP:** `api.track-iq.tech:7018`  
**APN:** `internet` (MTN Rwanda)  
**Password:** `123456` (device default, not used in most commands)

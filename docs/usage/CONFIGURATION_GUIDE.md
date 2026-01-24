# GPS Tracker - Configuration Guide

## Overview
This guide explains how to configure your GPS tracker to connect to your server and set various parameters.

---

## Configuration Methods

### Method 1: SMS Commands (Recommended)
Send configuration via text message from mobile phone.

### Method 2: USB AT Commands (Limited)
Send commands via USB serial connection (may not work on all models).

### Method 3: GPRS/Server Commands
Send commands from server after device connects.

---

## Important Notes

### Understanding AT Commands vs Binary Protocol

**Via USB Connection:**
- Device sends **binary GPS data** (0x7878 protocol)
- AT commands may not work over USB
- USB is for monitoring data, not configuration

**Via SMS (Phone):**
- Device accepts **AT commands** (AT+ZDR=...)
- This is the **recommended** configuration method
- Send as regular text message to device's SIM card

---

## SMS Configuration (Recommended Method)

### Prerequisites
1. Active SIM card installed in GPS tracker
2. SIM card has SMS capability
3. You know the device's phone number
4. Default password (usually: 123456)

### Common Configuration Commands

#### Set Server IP and Port

**Method 1: SERVER Command (Recommended for TK903ELE)**
```
SERVER,0,164.92.212.186,7018,0#
```
- Format: `SERVER,0,IP_ADDRESS,PORT,0#`
- No password required
- Replace `164.92.212.186` with your server IP
- Replace `7018` with your server port
- Device will reply: `server ok`

**Example:**
```
SERVER,0,api.gocavgo.com,7018,0#
```
or
```
SERVER,0,164.92.212.186,7018,0#
```

**Method 2: ADMINIP Command (Alternative)**
```
adminip123456 yourserver.com 7018
```
- Replace `123456` with your password
- Replace `yourserver.com` with your server address
- Replace `7018` with your server port
- Note: May not work on all firmware versions

#### Set APN (Mobile Data)
```
apn123456 internet.carrier.com username password
```
- Required for GPRS connection
- Get APN details from your mobile carrier
- Username/password may be optional

**Common APNs:**
- T-Mobile: `fast.t-mobile.com`
- AT&T: `phone`
- Verizon: `vzwinternet`

#### Set Upload Interval
```
fix020s060m***n123456
```
- Upload every 20 seconds when moving
- Upload every 60 minutes when stopped

#### Set Admin Phone Number
```
admin123456 15551234567
```
- Replace with your phone number
- Receives alarm notifications

#### Check Current Settings
```
check123456
```
- Returns current device configuration

#### Check IP and APN Settings
```
IPAPN123456
```
- Returns server IP, port, and APN settings

#### Reset Device
```
reset123456
```
- Reboots the GPS tracker

---

## USB Configuration (Limited Support)

### Using gps_config.py

**Interactive Mode:**
```bash
sudo python3 gps_config.py
# Select option 3 (Interactive mode)

AT> AT+ZDR=check123456
AT> AT+ZDR=adminip123456 yourserver.com 7018
AT> AT+ZDR=apn123456 internet.carrier.com user pass
AT> exit
```

**Automated Configuration:**
```bash
sudo python3 gps_config.py
# Select option 1 (G06L/G07L) or 2 (C32)
# Follow prompts
```

---

## Configuration Scenarios

### Scenario 1: First-Time Setup

**Goal:** Configure new GPS tracker to connect to your server

**Steps:**

1. **Insert SIM card** into GPS tracker
2. **Power on device** (12V)
3. **Wait for GSM connection** (LED blinking)
4. **Set admin phone** (optional):

```
admin123456 YOUR_PHONE_NUMBER
```

5. **Set server address (REQUIRED):**
```
SERVER,0,164.92.212.186,7018,0#
```

Reply: `server ok`

6. **Set APN (if using mobile data):**
```
APN,internet,,#
```
or
```
apn123456 internet
```

Reply: `apn ok`

7. **Set upload interval:**
```
fix020s060m***n123456
```

8. **Verify settings:**
```
check123456
```

9. **Device will respond with current configuration**

10. **Verify via USB (optional):**
```bash
sudo ./device_info.py
```
Check that Server shows: `164.92.212.186:7018`

---

### Scenario 2: Change Server Address

**To update server IP/domain:**

**Recommended Method:**
```
SERVER,0,164.92.212.186,7018,0#
```

**Alternative Method:**
```
adminip123456 newserver.com 8080
```

**Verify:**
```
check123456
```

**Or via USB:**
```bash
sudo ./device_info.py
# Check that Server line shows new address
```

**Note:** Settings take effect immediately, no reset needed

---

### Scenario 3: Enable/Disable Alarms

**Enable SOS alarm:**
```
KC123456 1
```

**Enable GPRS + SMS alarm:**
```
KC123456 2
```

**Enable GPRS + SMS + Call alarm:**
```
KC123456 3
```

**Cancel all alarms:**
```
KC123456 0
```

---

### Scenario 4: Fuel Cut/Resume (Optional)

**Cut fuel (stop vehicle):**
```
stop123456
```

**Resume fuel:**
```
resume123456
```

**Note:** Only works if installed with vehicle relay!

---

## G06L/G07L Command Reference

### Basic Commands

| Command | Description | Example |
|---------|-------------|---------|
| `SERVER,0,IP,PORT,0#` | Set server address (Recommended) | `SERVER,0,164.92.212.186,7018,0#` |
| `adminip123456 SERVER PORT` | Set server address (Alternative) | `adminip123456 gps.example.com 7018` |
| `APN,apn_name,,#` | Set APN settings | `APN,internet,,#` |
| `apn123456 APN USER PASS` | Set APN with password | `apn123456 internet.mobile.com web web` |
| `password123456 NEWPASS` | Change password | `password123456 888888` |
| `check123456` | Check settings | `check123456` |
| `IPAPN123456` | Check IP/APN | `IPAPN123456` |
| `reset123456` | Reset device | `reset123456` |

### TK903ELE Tested Commands (Firmware v1.1.6)

| Command | Status | Reply | Notes |
|---------|--------|-------|-------|
| `SERVER,0,IP,PORT,0#` | ✅ Working | `server ok` | Takes effect immediately |
| `APN,internet,,#` | ✅ Working | `apn ok` | For MTN Rwanda |
| `check123456` | ✅ Working | Full status | Returns all settings |
| `STATUS#` | ✅ Working | Device status | Battery, GPS, GSM info |
| `adminip123456 IP PORT` | ⚠️ Limited | No reply | USB only, not persistent |

### Interval Settings

| Command | Description |
|---------|-------------|
| `fix020s060m***n123456` | 20s moving, 60min stopped |
| `fix010s030m***n123456` | 10s moving, 30min stopped |
| `fix030s120m***n123456` | 30s moving, 120min stopped |

### Alarm Commands

| Command | Description |
|---------|-------------|
| `acc123456` | Enable ACC ON/OFF alarm |
| `noacc123456` | Disable ACC alarm |
| `move123456 0200` | Movement alarm (200m) |
| `nomove123456` | Cancel movement alarm |
| `vibrate123456 1` | Enable vibration alarm |
| `vibrate123456 0` | Disable vibration alarm |
| `lowbattery123456 on` | Enable low battery alarm |
| `lowbattery123456 off` | Disable low battery alarm |

### Advanced Settings

| Command | Description |
|---------|-------------|
| `admin123456 PHONE` | Set admin phone |
| `noadmin123456 PHONE` | Remove admin phone |
| `time zone123456 -5` | Set timezone (GMT-5) |
| `sleep123456 on` | Enable sleep mode |
| `sleep123456 off` | Disable sleep mode |
| `ANGLE123456 30` | Upload on 30° angle change |

---

## C32 Model Commands

### Command Format Difference

C32 uses slightly different format with `#` terminator:

| G06L Format | C32 Format |
|-------------|------------|
| `adminip123456 SERVER PORT` | `AT+ZDR=SERVER,1,SERVER,PORT,0#` |
| `apn123456 APN USER PASS` | `AT+ZDR=APN,APN,USER,PASS#` |
| `check123456` | `AT+ZDR=Status#` |
| `IPAPN123456` | `AT+ZDR=IP#` |

### C32 Examples

**Set Server:**
```
SERVER,1,gps.example.com,7018,0#
```

**Set APN:**
```
APN,internet.mobile.com,web,web#
```

**Check Status:**
```
Status#
```

---

## Verification and Testing

### After Configuration

1. **Check settings** (via SMS):
   ```
   check123456
   ```

2. **Verify server connection:**
   - Device should connect to your server
   - Check server logs for incoming connections
   - Look for login packet (0x01)

3. **Monitor USB data:**
   ```bash
   sudo python3 raw_monitor.py
   # Select option 4
   ```

4. **Check for GPS fix:**
   - Device should send location packets (0x12)
   - May take 2-5 minutes for first GPS fix

---

## Configuration Checklist

Before deploying GPS tracker:

- [ ] SIM card inserted and activated
- [ ] Default password known (usually 123456)
- [ ] Admin phone number set
- [ ] Server IP/domain configured
- [ ] Server port configured (e.g., 7018)
- [ ] APN settings configured (for mobile data)
- [ ] Upload interval set
- [ ] Settings verified with `check123456`
- [ ] Device reset after configuration
- [ ] Connection to server verified

---

## Troubleshooting Configuration

### SMS Commands Not Working

**Possible Causes:**
1. Wrong password
2. SIM card not active
3. No SMS balance
4. Wrong command format

**Solutions:**
- Verify password (try default: 123456)
- Check SIM card has SMS capability
- Ensure correct spacing in commands
- Wait 30 seconds between commands

---

### Device Not Connecting to Server

**Checklist:**
1. Is server IP/domain correct?
2. Is server port correct?
3. Is APN configured correctly?
4. Does SIM have data plan?
5. Is server actually running?

**Debug Steps:**
```bash
# 1. Verify settings via SMS
check123456
IPAPN123456

# 2. Check server is listening
netstat -tuln | grep 7018

# 3. Monitor device data
sudo python3 raw_monitor.py
```

---

### Cannot Change Settings

**If device rejects commands:**
1. Verify password is correct
2. Check command format (G06L vs C32)
3. Ensure proper spacing
4. Try resetting device first

**Factory Reset:**
```
begin123456
```

**Warning:** This erases all settings!

---

## Complete Configuration Example

### Real-World Setup: TK903ELE on GOCAVGO Server

**Device Details:**
- Model: TK903ELE v1.1.6
- IMEI: 868720064874575
- SIM: MTN Rwanda
- Server: 164.92.212.186 (api.gocavgo.com)

**Step-by-Step Configuration:**

1. **Check current status:**
   ```sms
   STATUS#
   ```
   
   Reply:
   ```
   V:4.22V,12V;CSQ:22;PWR:1;ACC:0;GPS:3,0,0;GS:0,1
   ```
   - Battery: 4.22V (100%)
   - GSM Signal: CSQ 22 (strong)
   - GPS: 3 satellites

2. **Configure server:**
   ```sms
   SERVER,0,164.92.212.186,7018,0#
   ```
   
   Reply:
   ```
   server ok
   ```

3. **Set APN:**
   ```sms
   APN,internet,,#
   ```
   
   Reply:
   ```
   apn ok
   ```

4. **Verify via USB:**
   ```bash
   sudo ./device_info.py
   ```
   
   Output shows:
   ```
   🌐 Server: 164.92.212.186:7018
   📶 GSM Signal: CSQ: 22
   🔋 Battery: 100%
   ```

5. **Check server connection:**
   On server (164.92.212.186):
   ```bash
   curl http://164.92.212.186:8000/api/devices/
   ```
   
   Returns:
   ```json
   [
     {
       "id": 1,
       "imei": "868720064874575",
       "name": "TK903ELE",
       "status": "online",
       "battery_level": "Very High",
       "gsm_signal": "Strong"
     }
   ]
   ```

6. **View location data:**
   ```bash
   curl http://164.92.212.186:8000/api/locations/1/latest
   ```
   
   Returns GPS coordinates, speed, and timestamp.

**Success!** Device is now tracking and sending data every 20 seconds when moving.

---

## Quick Reference Card

**For TK903ELE (Print and keep):**

| Task | SMS Command |
|------|-------------|
| Set Server | `SERVER,0,IP,PORT,0#` |
| Set APN | `APN,internet,,#` |
| Check Status | `STATUS#` |
| Check Settings | `check123456` |
| Change Password | `password123456 NEWPASS` |
| Reset Device | `reset123456` |

**Your Server:** 164.92.212.186:7018  
**APN:** internet (MTN Rwanda)  
**Default Password:** 123456  

---
## Security Recommendations

### Change Default Password

```
password123456 YOURNEWPASS
```

Then use new password in all commands:
```
adminip YOURNEWPASS server.com 7018
```

### Protect Admin Access

- Change default password immediately
- Keep password secure
- Limit admin phone numbers
- Use secure server connection

---

## Next Steps

After configuration:

1. **Build GPS Server** - Create TCP server to receive data
2. **Parse Protocol** - Decode binary GPS packets
3. **Store Data** - Save GPS coordinates to database
4. **Create Interface** - Build web UI to view locations

---

## Quick Reference

```bash
# Check device status
check123456

# Basic setup
admin123456 YOUR_PHONE
adminip123456 yourserver.com 7018
apn123456 internet.carrier.com
fix020s060m***n123456
reset123456

# Verify
check123456
IPAPN123456
```

---

**Configuration Complete! ✓**

Your GPS tracker is now ready to send data to your server.

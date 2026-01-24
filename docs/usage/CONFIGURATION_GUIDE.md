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
```
adminip123456 yourserver.com 7018
```
- Replace `123456` with your password
- Replace `yourserver.com` with your server address
- Replace `7018` with your server port

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
4. **Send SMS** from your phone:

```
admin123456 YOUR_PHONE_NUMBER
```

5. **Set server address:**
```
adminip123456 yourserver.com 7018
```

6. **Set APN (if using mobile data):**
```
apn123456 internet.yourcarrier.com
```

7. **Set upload interval:**
```
fix020s060m***n123456
```

8. **Verify settings:**
```
check123456
```

9. **Device will respond with current configuration**

---

### Scenario 2: Change Server Address

**To update server IP/domain:**

```
adminip123456 newserver.com 8080
```

**Verify:**
```
IPAPN123456
```

**Reset device to apply:**
```
reset123456
```

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
| `adminip123456 SERVER PORT` | Set server address | `adminip123456 gps.example.com 7018` |
| `apn123456 APN USER PASS` | Set APN settings | `apn123456 internet.mobile.com web web` |
| `password123456 NEWPASS` | Change password | `password123456 888888` |
| `check123456` | Check settings | `check123456` |
| `IPAPN123456` | Check IP/APN | `IPAPN123456` |
| `reset123456` | Reset device | `reset123456` |

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

# GPS Tracker - Connection Guide

## Overview
This guide explains how to connect to and test communication with your GPS tracker using the provided Python scripts.

---

## Quick Start

### Basic Connection Test

```bash
cd /home/leo/BYThron/Byt_gps_app
sudo python3 test_connection.py
```

This will:
- ✓ Test multiple baud rates
- ✓ Attempt communication with device
- ✓ Display detected baud rate
- ✓ Show any responses from device

---

## Available Scripts

### 1. device_info.py - Device Status Monitor

**Purpose:** Displays comprehensive GPS tracker information

**Usage:**
```bash
sudo ./device_info.py
sudo ./device_info.py --duration 60  # Monitor for 60 seconds
sudo ./device_info.py --debug        # Show raw messages
```

**What it displays:**
- 🔋 Battery voltage and percentage
- 🛰️  GPS lock status
- 📶 GSM signal quality (CSQ)
- 🚗 ACC status (moving/parked)
- 🌐 Server connection status
- 📱 Device serial number
- 💾 Firmware version
- 💾 Memory/storage (if available)

**Expected Output:**
```
📊 GPS TRACKER STATUS
============================================================

🔋 Battery Status:
   Voltage:    3.97V (3967mV)
   Level:      76.7%
   🟡 [███████████████░░░░░] 76.7%

🛰️  GPS:        📍 SEARCHING
📶 GSM Signal: 📶▆ (CSQ: 19)
🚗 ACC:        🅿️  OFF (Parked)

🌐 Server:     www.gps2828.com:7018
   Status:     ❌ Not Connected

📱 Serial:     868720064874575
💾 Firmware:   TK903ELE_VER_1.1.6
```

---

### 2. test_connection.py - Connection Tester

**Purpose:** Verifies serial communication with GPS tracker

**Usage:**
```bash
sudo python3 test_connection.py
```

**What it does:**
- Tests baud rates: 9600, 19200, 38400, 57600, 115200
- Sends test commands (AT+ZDR=check123456, AT, etc.)
- Listens for responses
- Reports detected baud rate

**Expected Output (Success):**
```
✓✓✓ CONNECTION SUCCESSFUL ✓✓✓
Correct baud rate: 57600
```

**Expected Output (Failure):**
```
✗ CONNECTION FAILED
Troubleshooting:
  1. Is the device powered on (12V)?
  2. Is the USB cable properly connected?
  ...
```

---

### 3. gps_config.py - Configuration Tool

**Purpose:** Configure GPS tracker settings via AT commands

**Usage:**
```bash
sudo python3 gps_config.py
```

**Menu Options:**
1. Configure G06L/G07L model
2. Configure C32 model
3. Interactive mode (custom commands)
4. Quick test

**Interactive Mode Example:**
```bash
sudo python3 gps_config.py
# Select option 3
AT> AT+ZDR=check123456
← Response: [device response]
```

---

### 3. raw_monitor.py - Raw Data Monitor

**Purpose:** Monitor raw binary data from GPS tracker

**Usage:**
```bash
sudo python3 raw_monitor.py
```

**Menu Options:**
1. Rapid 'Open Log' simulation
2. Passive listen at 115200 baud
3. Test all baud rates
4. Continuous monitor (real-time)
5. Quick combo test

**Continuous Monitor Example:**
```bash
sudo python3 raw_monitor.py
# Select option 4
# Press Ctrl+C to stop
```

---

## Connection Workflow

### First Time Setup

**Step 1: Hardware Connection**
```bash
# 1. Connect 12V power to GPS tracker
# 2. Connect USB cable (factory-provided)
# 3. Wait 5 seconds for device to initialize
```

**Step 2: Verify Detection**
```bash
ls /dev/ttyUSB*
# Should show: /dev/ttyUSB0
```

**Step 3: Check Device Status**
```bash
sudo ./device_info.py
```

**Step 4: Test Connection (if needed)**
```bash
sudo python3 test_connection.py
```

**Step 4: Record Baud Rate**
Note the detected baud rate from the test (typically 57600).

---

### Regular Usage

Once you know the correct baud rate:

**Quick Test:**
```bash
sudo python3 test_connection.py
```

**Configure Device:**
```bash
sudo python3 gps_config.py
# Follow menu prompts
```

**Monitor Data:**
```bash
sudo python3 raw_monitor.py
# Select option 4 for continuous monitoring
```

---

## Understanding Device Communication

### Communication Protocol

The GPS tracker uses **binary protocol** over USB, not AT commands!

- **USB Connection:** Sends binary GPS data packets (0x7878 format)
- **SMS/Phone:** Uses AT commands (AT+ZDR=...)

### Data Format

When connected via USB at 57600 baud, you'll see:
- Binary data (appears as garbled text)
- Protocol packets starting with 0x7878
- Location data (0x12), Login (0x01), Heartbeat (0x13), Alarms (0x16)

**Example Raw Output:**
```
8]
JHJ
8
JJHJ
@S
ȂJ8O
```

This is **normal** - it's binary GPS protocol data!

---

## Command Reference

### Manual Serial Connection

**Using screen:**
```bash
sudo screen /dev/ttyUSB0 57600
# Ctrl+A then D to detach
# Ctrl+A then K to kill
```

**Using minicom:**
```bash
sudo minicom -D /dev/ttyUSB0 -b 57600
# Ctrl+A then Z for help
# Ctrl+A then X to exit
```

**Using picocom:**
```bash
sudo picocom -b 57600 /dev/ttyUSB0
# Ctrl+A then Ctrl+X to exit
```

---

## Troubleshooting Connection Issues

### Issue: No response at any baud rate

**Checklist:**
1. ✓ Is 12V power connected and LED on?
2. ✓ Is USB cable the factory-provided one?
3. ✓ Did you wait 20 seconds after power-on?
4. ✓ Is `/dev/ttyUSB0` present?

**Solutions:**
```bash
# Power cycle device
# Disconnect power and USB, wait 10 seconds, reconnect

# Check USB detection
sudo dmesg | grep ch341

# Try different baud rate
sudo python3 raw_monitor.py
# Select option 3 (test all baud rates)
```

---

### Issue: Permission denied

**Solution 1: Use sudo**
```bash
sudo python3 test_connection.py
```

**Solution 2: Add user to dialout group**
```bash
sudo usermod -a -G dialout $USER
# Log out and log back in
```

**Verify:**
```bash
groups
# Should list 'dialout'
```

---

### Issue: Device detected but not responding

**Possible Causes:**
1. Wrong baud rate
2. Device in sleep mode
3. Device waiting for specific initialization

**Solutions:**
```bash
# Try rapid open/close (simulates SSCOM "Open Log" button)
sudo python3 raw_monitor.py
# Select option 1

# Power cycle within 20 seconds and retry
sudo python3 test_connection.py
```

---

### Issue: Getting binary data instead of AT responses

**This is normal!** USB connection uses binary protocol, not AT commands.

**Understanding:**
- AT commands: For SMS/phone configuration
- Binary protocol: For USB/GPRS communication

To configure via AT commands, use SMS from phone or special tools.

---

## Connection Scenarios

### Scenario 1: Initial Device Test

```bash
# 1. Connect hardware
# 2. Test connection
sudo python3 test_connection.py

# Expected: Baud rate detection (e.g., 57600)
```

---

### Scenario 2: Monitor Real-Time GPS Data

```bash
# Start continuous monitor
sudo python3 raw_monitor.py
# Select option 4

# You'll see binary GPS packets
# Press Ctrl+C when done
```

---

### Scenario 3: Send Configuration Commands

```bash
# Interactive mode
sudo python3 gps_config.py
# Select option 3 (Interactive)

# Type commands:
AT> AT+ZDR=check123456
AT> AT+ZDR=IPAPN123456
AT> exit
```

---

### Scenario 4: Automated Configuration

```bash
sudo python3 gps_config.py
# Select option 1 (G06L/G07L)

# Follow prompts:
Password: 123456
Server IP: yourserver.com
Server Port: 7018
APN: [your carrier APN]
...
```

---

## Tips and Best Practices

### Connection Tips

1. **Always power on device before USB connection**
2. **Wait 5-10 seconds after power-on**
3. **Use factory USB cable** (CH341 chip)
4. **Note your device's baud rate** (typically 57600)

### Testing Tips

1. **Start with test_connection.py** to verify basic communication
2. **Use raw_monitor.py** to see actual data being sent
3. **Use gps_config.py** only after confirming connection works

### Data Interpretation

When you see "garbled" data like `8]JHJ@S`, this means:
- ✓ Device is communicating
- ✓ Using binary protocol (correct for USB)
- ✓ Ready for server connection

---

## Next Steps

After successful connection:

1. **[Configuration Guide](CONFIGURATION_GUIDE.md)** - Configure GPS tracker settings
2. **Build GPS Server** - Create server to receive GPS data
3. **Parse Binary Protocol** - Decode location packets

---

## Quick Command Reference

```bash
# Test connection
sudo python3 test_connection.py

# Configure device
sudo python3 gps_config.py

# Monitor data
sudo python3 raw_monitor.py

# Check USB device
ls /dev/ttyUSB*

# View kernel logs
sudo dmesg | tail -20

# Manual serial connection
sudo screen /dev/ttyUSB0 57600
```

---

**Connection established! ✓**

You're now ready to configure your GPS tracker or build your server application.

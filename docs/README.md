# GPS Tracker System - Documentation

## Welcome

This documentation will guide you through setting up, connecting to, and configuring GPS trackers on Ubuntu 24.04.

---

## Quick Navigation

### 📦 [Installation Guide](installation/README.md)
Complete setup instructions for Ubuntu 24.04
- System requirements
- Package installation
- Driver setup
- Hardware connection
- Troubleshooting

### 🔌 [Connection Guide](usage/CONNECTION_GUIDE.md)
How to connect to and test your GPS tracker
- Using test scripts
- Serial communication
- Baud rate detection
- Monitoring data
- Connection troubleshooting

### ⚙️ [Configuration Guide](usage/CONFIGURATION_GUIDE.md)
Configure GPS tracker settings
- SMS commands
- Server configuration
- APN settings
- Alarm configuration
- Command reference

---

## Project Structure

```
Byt_gps_app/
├── docs/                          # Documentation
│   ├── README.md                  # This file
│   ├── installation/              # Installation guides
│   │   └── README.md
│   └── usage/                     # Usage guides
│       ├── CONNECTION_GUIDE.md
│       └── CONFIGURATION_GUIDE.md
│
├── device_info.py                 # Device status monitor
├── test_connection.py             # Connection testing script
├── gps_config.py                  # Configuration tool
├── analyze_protocol.py            # Protocol analyzer (debugging)
│
├── Other model How to config parameters.docx
└── G08L How to config parameters.docx
```

---

## Quick Start

### 1. Install Required Packages

```bash
sudo apt install -y minicom screen picocom python3-serial
```

### 2. Connect Hardware

- Connect 12V power to GPS tracker
- Connect USB cable to computer
- Wait for device initialization

### 3. Test Connection

```bash
cd /home/leo/BYThron/Byt_gps_app
sudo python3 test_connection.py
```

### 4. Expected Result

```
✓✓✓ CONNECTION SUCCESSFUL ✓✓✓
Correct baud rate: 57600
```

---

## Available Scripts

### device_info.py
Displays comprehensive device status and information
- Battery voltage and percentage
- GPS lock status
- GSM signal quality
- Server connection status
- Device serial number and firmware
- Memory/storage (if available)

**Usage:**
```bash
sudo ./device_info.py
sudo ./device_info.py --duration 60
```

### test_connection.py
Tests serial communication with GPS tracker
- Detects baud rate automatically
- Verifies device response
- Reports connection status

**Usage:**
```bash
sudo python3 test_connection.py
```

---

### gps_config.py
Configure GPS tracker settings
- Interactive command mode
- Automated configuration wizard
- Support for G06L/G07L and C32 models

**Usage:**
```bash
sudo python3 gps_config.py
```

---

### raw_monitor.py
Monitor raw binary data from device
- Real-time data display
- Multiple monitoring modes
- Baud rate testing

**Usage:**
```bash
sudo python3 raw_monitor.py
```

---

## Common Tasks

### Check if Device is Connected

```bash
ls /dev/ttyUSB*
```

Expected: `/dev/ttyUSB0`

---

### Test Communication

```bash
sudo python3 test_connection.py
```

---

### Configure via SMS

Send text message to GPS tracker's SIM:
```
adminip123456 yourserver.com 7018
apn123456 internet.carrier.com
check123456
```

---

### Monitor GPS Data

```bash
sudo python3 raw_monitor.py
# Select option 4 (Continuous monitor)
```

---

## Understanding GPS Tracker Communication

### Protocol Types

**Binary Protocol (USB/GPRS):**
- Used for GPS data transmission
- Format: 0x7878 + packet data + 0x0D0A
- Packets: Login (0x01), Location (0x12), Heartbeat (0x13), Alarm (0x16)

**AT Commands (SMS):**
- Used for configuration
- Format: `command123456 parameters`
- Examples: `adminip123456`, `check123456`, `reset123456`

### Data Flow

```
GPS Tracker → Binary GPS Data → Your Server
           ← Configuration (SMS) ← Phone
```

---

## Supported GPS Tracker Models

- G06L / G07L
- C32
- Compatible models using same protocol

---

## System Requirements

### Hardware
- Ubuntu 24.04 or similar Linux distribution
- GPS Tracker with CH341 USB chip
- 12V power supply
- Factory-provided USB cable

### Software
- Python 3.8+
- pyserial library
- Serial terminal tools (minicom/screen)

---

## Troubleshooting

### Device Not Detected

```bash
# Check USB connection
lsusb

# Check kernel messages
sudo dmesg | grep ch341

# Verify device file exists
ls -l /dev/ttyUSB*
```

---

### No Response from Device

1. Check 12V power is connected
2. Wait 20 seconds after power-on
3. Try different baud rate
4. Use raw_monitor.py to see any data

---

### Permission Denied

```bash
# Add user to dialout group
sudo usermod -a -G dialout $USER

# Log out and back in, or use sudo
sudo python3 test_connection.py
```

---

## Next Steps

### For First-Time Users

1. ✓ [Install system packages](installation/README.md)
2. ✓ [Connect and test device](usage/CONNECTION_GUIDE.md)
3. ✓ [Configure GPS tracker](usage/CONFIGURATION_GUIDE.md)
4. → Build GPS tracking server
5. → Create web interface

### For Developers

- **Parse binary protocol** - Decode GPS packets
- **Build TCP server** - Listen on port 7018
- **Store GPS data** - Database integration
- **Create API** - REST API for location queries
- **Web interface** - Real-time map display

---

## Additional Resources

### Documentation Files
- `Other model How to config parameters.docx` - Configuration manual
- `G08L How to config parameters.docx` - Model-specific guide
- GPS Protocol Specification (from original PDF)

### Useful Links
- CH341 Linux driver: Built into kernel (v2.6.24+)
- pyserial documentation: https://pyserial.readthedocs.io/
- GPS coordinate formats: WGS84 standard

---

## Support

### Getting Help

1. Check relevant documentation section
2. Review troubleshooting guides
3. Verify hardware connections
4. Check system logs: `sudo dmesg`

### Common Questions

**Q: Do I need Windows drivers on Ubuntu?**  
A: No, CH341 driver is built into Linux kernel.

**Q: Can I configure via USB instead of SMS?**  
A: Limited support. SMS is recommended method.

**Q: What baud rate should I use?**  
A: Run `test_connection.py` to auto-detect (usually 57600).

**Q: Why am I seeing binary/garbled data?**  
A: Normal! GPS uses binary protocol over USB.

---

## Documentation Updates

Last Updated: January 12, 2026  
Version: 1.0

---

**Ready to get started?**

Begin with the [Installation Guide](installation/README.md) →

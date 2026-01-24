# GPS Tracker System - Installation Guide

## Overview
This guide will help you set up the GPS tracker system on Ubuntu 24.04. The system allows you to communicate with GPS trackers, configure them, and eventually build a server to receive GPS data.

---

## System Requirements

### Hardware
- Ubuntu 24.04 (or similar Linux distribution)
- GPS Tracker device (G06L/G07L/C32 or compatible models)
- 12V power supply for GPS tracker
- Factory-provided USB cable (CH341 chip-based)

### Software
- Python 3.8 or higher
- USB serial drivers (CH341 - built into Linux kernel)
- Internet connection (for installing packages)

---

## Installation Steps

### 1. Update System Packages

```bash
sudo apt update
sudo apt upgrade -y
```

### 2. Install Required Tools

```bash
sudo apt install -y minicom screen picocom python3-serial
```

**Package Descriptions:**
- `minicom` - Terminal-based serial communication tool
- `screen` - Simple serial terminal
- `picocom` - Lightweight serial terminal
- `python3-serial` - Python library for serial communication

### 3. Verify USB Driver Installation

The CH341 USB-to-serial driver is already included in Ubuntu 24.04 kernel. Verify it's working:

```bash
# Check kernel module
lsmod | grep ch341

# Expected output: ch341 module should be listed
```

### 4. Set Up User Permissions

Add your user to the `dialout` group to access serial ports without sudo:

```bash
sudo usermod -a -G dialout $USER
```

**Important:** After running this command, you must **log out and log back in** for the changes to take effect.

To verify:
```bash
groups
# Should see 'dialout' in the list
```

### 5. Install Project Scripts

The project includes the following Python scripts:

1. **test_connection.py** - Tests connection to GPS tracker
2. **gps_config.py** - Configures GPS tracker settings
3. **raw_monitor.py** - Raw data monitoring tool

All scripts are located in: `/home/leo/BYThron/Byt_gps_app/`

Make scripts executable:
```bash
cd /home/leo/BYThron/Byt_gps_app
chmod +x test_connection.py gps_config.py raw_monitor.py
```

---

## Hardware Setup

### 1. Connect GPS Tracker

Follow these steps **in order**:

1. **Power Connection:**
   - Connect 12V power supply to GPS tracker
   - Ensure proper polarity (red = positive, black = ground)
   - Device LED should turn on (if present)

2. **USB Connection:**
   - Use the **factory-provided USB cable** (not a standard USB cable)
   - Connect GPS tracker to your Ubuntu computer
   - Wait 2-3 seconds for device recognition

3. **Verify Connection:**
   ```bash
   ls /dev/ttyUSB*
   ```
   Expected output: `/dev/ttyUSB0` (or similar)

4. **Check Kernel Messages:**
   ```bash
   sudo dmesg | tail -20
   ```
   Look for messages like:
   ```
   ch341-uart converter detected
   usb X-X: ch341-uart converter now attached to ttyUSB0
   ```

---

## Troubleshooting

### Problem: `/dev/ttyUSB0` not found

**Solutions:**
1. Check USB cable connection
2. Try a different USB port
3. Check if device is powered (12V)
4. Verify kernel messages: `sudo dmesg | grep ch341`

### Problem: Permission denied when accessing serial port

**Solutions:**
1. Add user to dialout group:
   ```bash
   sudo usermod -a -G dialout $USER
   ```
2. Log out and log back in
3. Or run scripts with `sudo`

### Problem: Device not responding

**Solutions:**
1. Ensure 12V power is connected
2. Wait 20 seconds after power-on before testing
3. Verify correct baud rate (57600 for most devices)
4. Try power cycling the device

### Problem: Wrong baud rate

**Test all baud rates:**
```bash
sudo python3 test_connection.py
```
The script will automatically detect the correct baud rate.

---

## Verification

After installation, verify everything is working:

```bash
# 1. Check device is detected
ls /dev/ttyUSB*

# 2. Run connection test
sudo python3 test_connection.py

# Expected output: "CONNECTION SUCCESSFUL" with detected baud rate
```

---

## Next Steps

Once installation is complete, proceed to:
- **[Connection Guide](../usage/CONNECTION_GUIDE.md)** - How to use the scripts
- **[Configuration Guide](../usage/CONFIGURATION_GUIDE.md)** - How to configure GPS tracker

---

## Additional Resources

### Documentation Files
- Protocol specification: `/home/leo/BYThron/Byt_gps_app/GPS_Protocol.txt` (from manual)
- Configuration manual: `/home/leo/BYThron/Byt_gps_app/Other model How to config parameters.docx`

### Useful Linux Commands
```bash
# Monitor USB events in real-time
sudo dmesg -w

# Check serial port status
ls -l /dev/ttyUSB*

# View connected USB devices
lsusb

# Manual serial connection (115200 baud)
sudo minicom -D /dev/ttyUSB0 -b 57600
```

---

## Support

### Common Issues
1. **No response from device** - Check power and USB cable
2. **Permission errors** - Add user to dialout group
3. **Driver issues** - CH341 driver is built-in, should work automatically

### Getting Help
- Review troubleshooting section above
- Check kernel logs: `sudo dmesg | tail -50`
- Verify hardware connections
- Test with raw_monitor.py for detailed diagnostics

---

**Installation Complete! ✓**

You're now ready to connect to and configure your GPS tracker.

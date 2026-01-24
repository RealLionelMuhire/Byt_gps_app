#!/usr/bin/env python3
"""
Quick GPS Tracker Connection Test
Tests serial communication with the GPS device
"""

import serial
import time
import sys

def test_connection(port='/dev/ttyUSB0', baudrates=[9600, 19200, 38400, 57600, 115200]):
    """Test connection at different baud rates"""
    
    print("="*60)
    print("GPS Tracker Connection Test")
    print("="*60)
    print(f"\nTesting port: {port}")
    print(f"Will try baud rates: {baudrates}\n")
    
    for baudrate in baudrates:
        print(f"\n--- Testing at {baudrate} baud ---")
        
        try:
            ser = serial.Serial(
                port=port,
                baudrate=baudrate,
                timeout=2,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE
            )
            
            print(f"✓ Port opened at {baudrate} baud")
            
            # Clear buffers
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            time.sleep(0.5)
            
            # Try different test commands
            test_commands = [
                "AT+ZDR=check123456\r\n",
                "AT\r\n",
                "check123456\r\n"
            ]
            
            for cmd in test_commands:
                print(f"  → Sending: {cmd.strip()}")
                ser.write(cmd.encode('utf-8'))
                ser.flush()
                time.sleep(1)
                
                # Read any response
                if ser.in_waiting > 0:
                    response = ser.read(ser.in_waiting)
                    try:
                        decoded = response.decode('utf-8', errors='ignore').strip()
                        if decoded:
                            print(f"  ← Response: {decoded}")
                            print(f"\n✓✓✓ SUCCESS! Device responding at {baudrate} baud ✓✓✓")
                            ser.close()
                            return baudrate
                    except:
                        pass
            
            # Also check if device is sending data automatically
            print("  Waiting for automatic data...")
            time.sleep(2)
            if ser.in_waiting > 0:
                data = ser.read(ser.in_waiting)
                print(f"  ← Received data: {data[:100]}...")  # Show first 100 bytes
                print(f"\n✓ Device is sending data at {baudrate} baud!")
                ser.close()
                return baudrate
            
            print(f"  ✗ No response at {baudrate} baud")
            ser.close()
            
        except serial.SerialException as e:
            print(f"  ✗ Failed to open port: {e}")
            return None
        except Exception as e:
            print(f"  ✗ Error: {e}")
    
    print("\n✗ No response at any baud rate")
    print("\nTroubleshooting:")
    print("  1. Is the device powered on (12V)?")
    print("  2. Is the USB cable properly connected?")
    print("  3. Wait 20 seconds after power-on, then click 'Open Log' rapidly")
    print("     (as mentioned in the manual)")
    print("  4. Try running the script again")
    return None

def check_device_permissions(port='/dev/ttyUSB0'):
    """Check if user has permission to access the device"""
    import os
    import grp
    
    print("\n--- Checking Permissions ---")
    
    if not os.path.exists(port):
        print(f"✗ Device {port} not found!")
        print("  Make sure the GPS tracker is connected.")
        return False
    
    print(f"✓ Device {port} exists")
    
    # Check if user is in dialout group
    try:
        dialout_gid = grp.getgrnam('dialout').gr_gid
        user_groups = os.getgroups()
        
        if dialout_gid in user_groups:
            print(f"✓ User is in 'dialout' group")
            return True
        else:
            print(f"⚠ User is NOT in 'dialout' group")
            print(f"  Add yourself with: sudo usermod -a -G dialout $USER")
            print(f"  Then log out and back in")
            print(f"  Or run this script with: sudo python3 {sys.argv[0]}")
            return False
    except Exception as e:
        print(f"⚠ Could not check group membership: {e}")
        return False

if __name__ == "__main__":
    import os
    
    # Check if running as root
    if os.geteuid() == 0:
        print("✓ Running with sudo privileges\n")
    else:
        check_device_permissions()
        print("\nNote: If test fails, try running with: sudo python3 test_connection.py\n")
    
    result = test_connection()
    
    if result:
        print("\n" + "="*60)
        print(f"✓✓✓ CONNECTION SUCCESSFUL ✓✓✓")
        print(f"Correct baud rate: {result}")
        print("="*60)
        print("\nNext steps:")
        print("  1. Run: sudo python3 gps_config.py")
        print("  2. Configure your server IP and port")
        print("  3. Start building your GPS tracking server!")
    else:
        print("\n" + "="*60)
        print("✗ CONNECTION FAILED")
        print("="*60)
        print("\nPlease check:")
        print("  1. 12V power supply is connected")
        print("  2. USB cable is the factory-provided one")
        print("  3. Device LED is blinking (if it has one)")
        print("  4. Wait 20 seconds after power-on")

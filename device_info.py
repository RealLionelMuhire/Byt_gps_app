#!/usr/bin/env python3
"""
Device Info Monitor for GPS Tracker (ASCII Status Format)
Monitors device status and GPS location from tracker
"""

import serial
import time
import sys
import argparse
import re
import requests
from datetime import datetime

# Configuration
FORCE_SOUTHERN_HEMISPHERE = True  # Set to True for Rwanda/Southern Africa if device reports North

class BatteryMonitorASCII:
    """Monitor battery status from ASCII status messages"""
    
    def __init__(self, port='/dev/ttyUSB0', baudrate=115200, timeout=1, server_url=None):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None
        self.server_url = server_url or "https://api.track-iq.tech"
    
    def connect(self):
        """Connect to the GPS tracker, auto-detecting baud rate if needed."""
        # Try the configured baud rate first, then fall back to common rates
        baud_rates = [self.baudrate] if self.baudrate else []
        for rate in [9600, 115200, 19200, 38400, 57600]:
            if rate not in baud_rates:
                baud_rates.append(rate)

        for rate in baud_rates:
            try:
                ser = serial.Serial(
                    port=self.port,
                    baudrate=rate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=2
                )
                # Send a probe command and wait for any reply
                ser.write(b'AT+ZDR=check123456\r\n')
                time.sleep(0.5)
                if ser.in_waiting > 0:
                    self.baudrate = rate
                    self.ser = ser
                    self.ser.timeout = self.timeout
                    return True
                # Also accept if device sends unsolicited data (binary protocol)
                ser.write(b'AT\r\n')
                time.sleep(0.5)
                if ser.in_waiting > 0:
                    self.baudrate = rate
                    self.ser = ser
                    self.ser.timeout = self.timeout
                    return True
                ser.close()
            except serial.SerialException as e:
                print(f"❌ Error connecting to {self.port} at {rate}: {e}")
                return False

        print(f"❌ No response at any baud rate on {self.port}")
        print("   → G900LS J16-4G: USB port is power-only. Configure via SMS instead.")
        print("   → TK903ELE: Check the device is powered on (12V) and USB cable is connected.")
        return False
    
    def parse_status_message(self, message):
        """Parse ASCII status message from GPS tracker"""
        info = {}
        
        # Extract all fields using regex
        patterns = {
            'datetime': r'<(\d{4}-\d{2}-\d{2},\d{2}:\d{2}:\d{2})',
            'sim': r'SIM:(\d+),(\d+)',
            'csq': r'CSQ:(\d+)',
            'gps': r'GPS:(\d+)',
            'gps_data': r'GPS:([-\d.]+),([-\d.]+),(\d+)',  # GPS:lat,lon,satellites
            'av': r'AV:([AV])',
            'sv': r'SV:(\d+)',
            'pd': r'PD:([\d.]+)',
            'sn': r'SN:(\d+)',
            'addr': r'ADDR:([^,]+),(\d+),(\d+)',
            'apn': r'APN:([^;]*)',
            'user': r'USER:([^;]*)',
            'pwd': r'PWD:([^;]*)',
            'vol': r'VOL:(\d+),(\d+)',
            'gs': r'GS:(\d+),(\d+)',
            'pwr': r'PWR:(\d+)',
            'acc': r'ACC:(\d+)',
            'sos': r'SOS:(\d+)',
            'in': r'IN:(\d+)',
            'login': r'LOGIN:(\d+)',
            'version': r'(TK\w+_VER_[\d.]+)',
            'mem': r'MEM:(\d+),(\d+)',  # Memory: used,total (KB)
            'ram': r'RAM:(\d+)',  # RAM usage (%)
            'storage': r'STORAGE:(\d+),(\d+)',  # Storage: used,total (KB)
            'flash': r'FLASH:(\d+)',  # Flash usage (%)
            'lat': r'LAT:([-\d.]+)',  # Latitude
            'lon': r'LON:([-\d.]+)',  # Longitude
            'speed': r'SPEED:([\d.]+)',  # Speed in km/h
            'course': r'COURSE:([\d.]+)',  # Direction in degrees
        }
        
        for field, pattern in patterns.items():
            match = re.search(pattern, message)
            if match:
                if field == 'vol':
                    # VOL:3840,1 means 3840mV, battery status 1
                    info['voltage_mv'] = int(match.group(1))
                    info['battery_status'] = int(match.group(2))
                    info['voltage_v'] = info['voltage_mv'] / 1000.0
                elif field == 'csq':
                    info['signal_quality'] = int(match.group(1))
                elif field == 'gps':
                    info['gps_locked'] = int(match.group(1)) == 1
                elif field == 'gps_data':
                    # GPS:lat,lon,satellites format
                    lat = float(match.group(1))
                    if FORCE_SOUTHERN_HEMISPHERE and lat > 0:
                        lat = -lat
                    info['latitude'] = lat
                    info['longitude'] = float(match.group(2))
                    info['satellites'] = int(match.group(3))
                    info['gps_locked'] = True
                elif field == 'lat':
                    lat = float(match.group(1))
                    if FORCE_SOUTHERN_HEMISPHERE and lat > 0:
                        lat = -lat
                    info['latitude'] = lat
                elif field == 'lon':
                    info['longitude'] = float(match.group(1))
                elif field == 'speed':
                    info['speed_kmh'] = float(match.group(1))
                elif field == 'course':
                    info['course_deg'] = float(match.group(1))
                elif field == 'acc':
                    info['acc_on'] = int(match.group(1)) == 1
                elif field == 'addr':
                    info['server'] = match.group(1)
                    info['port'] = int(match.group(2))
                elif field == 'sn':
                    info['serial_number'] = match.group(1)
                elif field == 'version':
                    info['firmware'] = match.group(1)
                elif field == 'mem':
                    info['mem_used_kb'] = int(match.group(1))
                    info['mem_total_kb'] = int(match.group(2))
                    info['mem_used_percent'] = (info['mem_used_kb'] / info['mem_total_kb']) * 100
                elif field == 'ram':
                    info['ram_percent'] = int(match.group(1))
                elif field == 'storage':
                    info['storage_used_kb'] = int(match.group(1))
                    info['storage_total_kb'] = int(match.group(2))
                    info['storage_used_percent'] = (info['storage_used_kb'] / info['storage_total_kb']) * 100
                elif field == 'flash':
                    info['flash_percent'] = int(match.group(1))
                elif field == 'datetime':
                    info['device_time'] = match.group(1)
                elif field == 'login':
                    info['logged_in'] = int(match.group(1)) == 1
        
        return info if info else None
    
    def get_gps_from_server(self, imei):
        """Fetch latest GPS location from server"""
        try:
            # Get device ID from IMEI (try with and without leading zero)
            response = requests.get(f"{self.server_url}/api/devices/", timeout=5)
            if response.status_code == 200:
                devices = response.json()
                device_id = None
                for device in devices:
                    device_imei = device.get('imei', '')
                    # Match with or without leading zero
                    if device_imei == imei or device_imei == f"0{imei}" or device_imei.lstrip('0') == imei.lstrip('0'):
                        device_id = device.get('id')
                        break
                
                if device_id:
                    # Get latest location
                    response = requests.get(
                        f"{self.server_url}/api/locations/{device_id}/latest",
                        timeout=5
                    )
                    if response.status_code == 200:
                        location = response.json()
                        lat = location.get('latitude')
                        if lat is not None and FORCE_SOUTHERN_HEMISPHERE and lat > 0:
                            lat = -lat
                            
                        return {
                            'latitude': lat,
                            'longitude': location.get('longitude'),
                            'speed_kmh': location.get('speed', 0),
                            'course_deg': location.get('course', 0),
                            'satellites': location.get('satellites', 0),
                            'timestamp': location.get('timestamp'),
                            'gps_locked': True
                        }
        except Exception as e:
            # Silently fail if server is not available
            pass
        return None
    
    def get_battery_percentage(self, voltage_mv):
        """Estimate battery percentage from voltage (LiPo 3.7V battery)"""
        # LiPo voltage ranges:
        # 4.2V = 100% (fully charged)
        # 3.7V = 50% (nominal)
        # 3.5V = 20%
        # 3.3V = 5%
        # 3.0V = 0% (discharged)
        
        if voltage_mv >= 4200:
            return 100
        elif voltage_mv >= 4000:
            return 80 + ((voltage_mv - 4000) / 200) * 20
        elif voltage_mv >= 3700:
            return 50 + ((voltage_mv - 3700) / 300) * 30
        elif voltage_mv >= 3500:
            return 20 + ((voltage_mv - 3500) / 200) * 30
        elif voltage_mv >= 3300:
            return 5 + ((voltage_mv - 3300) / 200) * 15
        elif voltage_mv >= 3000:
            return ((voltage_mv - 3000) / 300) * 5
        else:
            return 0
    
    def display_status(self, info):
        """Display battery and device status"""
        
        # Try to get GPS data from server if IMEI is available
        if 'serial_number' in info and not ('latitude' in info and 'longitude' in info):
            gps_data = self.get_gps_from_server(info['serial_number'])
            if gps_data:
                info.update(gps_data)
        
        print("\n" + "=" * 60)
        print("📊 GPS TRACKER STATUS")
        print("=" * 60)
        
        # Battery Information
        if 'voltage_mv' in info:
            voltage_v = info['voltage_v']
            percentage = self.get_battery_percentage(info['voltage_mv'])
            
            print(f"\n🔋 Battery Status:")
            print(f"   Voltage:    {voltage_v:.2f}V ({info['voltage_mv']}mV)")
            print(f"   Level:      {percentage:.1f}%")
            
            # Visual battery indicator
            bar_length = 20
            filled = int((percentage / 100) * bar_length)
            bar = "█" * filled + "░" * (bar_length - filled)
            
            # Color indicator
            if percentage >= 80:
                indicator = "🟢"
            elif percentage >= 50:
                indicator = "🟡"
            elif percentage >= 20:
                indicator = "🟠"
            else:
                indicator = "🔴"
            
            print(f"   {indicator} [{bar}] {percentage:.1f}%")
        
        # GPS Status
        if 'gps_locked' in info:
            gps_icon = "🛰️  LOCKED" if info['gps_locked'] else "📍 SEARCHING"
            print(f"\n🛰️  GPS:        {gps_icon}")
            
            # Show GPS coordinates if available
            if 'latitude' in info and 'longitude' in info:
                lat = info['latitude']
                lon = info['longitude']
                
                # Format coordinates in degrees, minutes, seconds
                lat_deg = int(abs(lat))
                lat_min = int((abs(lat) - lat_deg) * 60)
                lat_sec = ((abs(lat) - lat_deg) * 60 - lat_min) * 60
                lat_dir = 'N' if lat >= 0 else 'S'
                
                lon_deg = int(abs(lon))
                lon_min = int((abs(lon) - lon_deg) * 60)
                lon_sec = ((abs(lon) - lon_deg) * 60 - lon_min) * 60
                lon_dir = 'E' if lon >= 0 else 'W'
                
                print(f"📍 Location:   {lat:.6f}°{lat_dir}, {lon:.6f}°{lon_dir}")
                print(f"              {lat_deg}°{lat_min}'{lat_sec:.1f}\"{lat_dir}, {lon_deg}°{lon_min}'{lon_sec:.1f}\"{lon_dir}")
                
                # Show timestamp if available
                if 'timestamp' in info:
                    timestamp_str = info['timestamp']
                    try:
                        # Parse ISO timestamp
                        timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                        time_ago = datetime.now(timestamp.tzinfo) - timestamp
                        seconds_ago = int(time_ago.total_seconds())
                        
                        if seconds_ago < 60:
                            time_str = f"{seconds_ago}s ago"
                        elif seconds_ago < 3600:
                            time_str = f"{seconds_ago // 60}m ago"
                        elif seconds_ago < 86400:
                            time_str = f"{seconds_ago // 3600}h ago"
                        else:
                            time_str = f"{seconds_ago // 86400}d ago"
                        
                        print(f"⏰ Last Update: {time_str} ({timestamp.strftime('%Y-%m-%d %H:%M:%S')})")
                    except:
                        print(f"⏰ Last Update: {timestamp_str}")
                
                # Google Maps link
                maps_url = f"https://www.google.com/maps?q={lat},{lon}"
                print(f"🗺️  Maps:       {maps_url}")
                
            # Show satellites if available
            if 'satellites' in info:
                sat_count = info['satellites']
                sat_icon = "🛰️ " * min(sat_count, 5)  # Show up to 5 satellite icons
                print(f"🛰️  Satellites: {sat_icon} ({sat_count})")
            
            # Show speed if available
            if 'speed_kmh' in info:
                speed = info['speed_kmh']
                speed_icon = "🚗" if speed > 0 else "🅿️ "
                print(f"{speed_icon} Speed:      {speed:.1f} km/h")
            
            # Show heading if available
            if 'course_deg' in info:
                course = info['course_deg']
                # Convert degrees to cardinal direction
                directions = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']
                idx = int((course + 22.5) / 45) % 8
                direction = directions[idx]
                print(f"🧭 Heading:    {course:.1f}° ({direction})")
        
        # GSM Signal
        if 'signal_quality' in info:
            csq = info['signal_quality']
            bars = min(4, csq // 7)  # CSQ 0-31, map to 0-4 bars
            signal_bar = "📶" + "▂▄▆█"[bars] if bars > 0 else "📵"
            print(f"📶 GSM Signal: {signal_bar} (CSQ: {csq})")
        
        # ACC Status
        if 'acc_on' in info:
            acc_status = "🚗 ON (Moving)" if info['acc_on'] else "🅿️  OFF (Parked)"
            print(f"🚗 ACC:        {acc_status}")
        
        # Server Connection
        if 'server' in info:
            login_status = "✅ Connected" if info.get('logged_in', False) else "❌ Not Connected"
            print(f"\n🌐 Server:     {info['server']}:{info['port']}")
        # Memory and Storage Information
        has_mem_info = False
        
        if 'mem_used_kb' in info:
            if not has_mem_info:
                print(f"\n💾 Memory & Storage:")
                has_mem_info = True
            mem_used_mb = info['mem_used_kb'] / 1024
            mem_total_mb = info['mem_total_kb'] / 1024
            mem_percent = info['mem_used_percent']
            print(f"   Memory:     {mem_used_mb:.1f}MB / {mem_total_mb:.1f}MB ({mem_percent:.1f}%)")
        
        if 'ram_percent' in info:
            if not has_mem_info:
                print(f"\n💾 Memory & Storage:")
                has_mem_info = True
            ram_percent = info['ram_percent']
            print(f"   RAM Usage:  {ram_percent}%")
        
        if 'storage_used_kb' in info:
            if not has_mem_info:
                print(f"\n💾 Memory & Storage:")
                has_mem_info = True
            storage_used_mb = info['storage_used_kb'] / 1024
            storage_total_mb = info['storage_total_kb'] / 1024
            storage_percent = info['storage_used_percent']
            print(f"   Storage:    {storage_used_mb:.1f}MB / {storage_total_mb:.1f}MB ({storage_percent:.1f}%)")
        
        if 'flash_percent' in info:
            if not has_mem_info:
                print(f"\n💾 Memory & Storage:")
                has_mem_info = True
            flash_percent = info['flash_percent']
            print(f"   Flash Usage: {flash_percent}%")
        
            print(f"   Status:     {login_status}")
        
        # Device Information
        if 'serial_number' in info:
            print(f"\n📱 Serial:     {info['serial_number']}")
        
        if 'firmware' in info:
            print(f"💾 Firmware:   {info['firmware']}")
        
        if 'device_time' in info:
            print(f"⏰ Device Time: {info['device_time']}")
        
        print(f"\n🕐 System Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)
    
    def monitor(self, duration=30, debug=False):
        """Monitor the GPS tracker for status messages"""
        print("🔍 Connecting to GPS tracker...")
        
        if not self.connect():
            return False
        
        print(f"✅ Connected at {self.baudrate} baud!")
        print(f"⏱️  Will monitor for {duration} seconds...")
        print("   Waiting for status messages...\n")
        
        if debug:
            print("🐛 DEBUG MODE: Showing raw messages\n")
        
        start_time = time.time()
        message_count = 0
        buffer = ""
        
        try:
            while (time.time() - start_time) < duration:
                if self.ser.in_waiting > 0:
                    # Read data and decode as ASCII
                    data = self.ser.read(self.ser.in_waiting).decode('ascii', errors='ignore')
                    buffer += data
                    
                    # Look for complete messages (enclosed in < >)
                    while '<' in buffer and '>' in buffer:
                        start = buffer.index('<')
                        end = buffer.index('>', start) + 1
                        message = buffer[start:end]
                        buffer = buffer[end:]
                        
                        message_count += 1
                        
                        if debug:
                            print(f"📨 Message #{message_count}:")
                            print(f"   {message[:100]}{'...' if len(message) > 100 else ''}\n")
                        
                        # Parse the message
                        info = self.parse_status_message(message)
                        
                        if info:
                            self.display_status(info)
                            
                            if not debug:
                                print("\n💡 Press Ctrl+C to exit")
                        else:
                            if debug:
                                print("   ⚠️  Could not parse message\n")
                
                time.sleep(0.1)
            
            # Timeout
            if message_count == 0:
                print("\n❌ No status messages received")
                print("\n💡 Troubleshooting:")
                print("   1. TK903ELE: Is the device powered on (12V)?")
                print("   2. G900LS J16-4G: USB is power-only — no serial data. Use SMS to configure.")
                print("   3. Try: sudo ./test_connection.py")
                print("   4. Try a specific baud rate: sudo ./device_info.py --baud 9600")
            else:
                print(f"\n✅ Received {message_count} status messages")
            
            return message_count > 0
            
        except KeyboardInterrupt:
            print("\n\n👋 Monitoring stopped by user")
            return True
        finally:
            if self.ser:
                self.ser.close()

def main():
    parser = argparse.ArgumentParser(
        description='Monitor GPS tracker device info and location',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  sudo ./device_info.py
  sudo ./device_info.py --duration 60
  sudo ./device_info.py --debug
  sudo ./device_info.py --server https://api.track-iq.tech
        """
    )
    
    parser.add_argument(
        '--port',
        default='/dev/ttyUSB0',
        help='Serial port (default: /dev/ttyUSB0)'
    )
    
    parser.add_argument(
        '--baud',
        type=int,
        default=None,
        help='Baud rate (default: auto-detect). TK903ELE is typically 9600.'
    )
    
    parser.add_argument(
        '--duration',
        type=int,
        default=30,
        help='Monitoring duration in seconds (default: 30)'
    )
    
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Show debug information'
    )
    
    parser.add_argument(
        '--server',
        default='https://api.track-iq.tech',
        help='GPS tracking server URL (default: https://api.track-iq.tech)'
    )
    
    args = parser.parse_args()
    
    monitor = BatteryMonitorASCII(
        port=args.port,
        baudrate=args.baud,
        server_url=args.server
    )
    
    success = monitor.monitor(duration=args.duration, debug=args.debug)
    sys.exit(0 if success else 1)

if __name__ == '__main__':
    main()

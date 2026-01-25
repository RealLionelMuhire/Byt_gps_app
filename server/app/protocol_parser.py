"""
GPS Tracker Binary Protocol Parser
Parses binary packets from GPS trackers (0x7878...0x0D0A format)
"""

import struct
from datetime import datetime
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


class ProtocolParser:
    """Parse GPS tracker binary protocol packets"""
    
    # Packet types
    PACKET_LOGIN = 0x01
    PACKET_LOCATION = 0x12
    PACKET_HEARTBEAT = 0x13
    PACKET_ALARM = 0x16
    PACKET_COMMAND = 0x80
    
    # Start and stop markers
    START_BIT = b'\x78\x78'
    STOP_BIT = b'\x0d\x0a'
    
    # Battery levels mapping
    BATTERY_LEVELS = {
        0x00: {"level": "No Power", "percent": 0},
        0x01: {"level": "Extremely Low", "percent": 10},
        0x02: {"level": "Very Low", "percent": 25},
        0x03: {"level": "Low", "percent": 40},
        0x04: {"level": "Medium", "percent": 60},
        0x05: {"level": "High", "percent": 80},
        0x06: {"level": "Very High", "percent": 100},
    }
    
    # GSM signal levels
    GSM_SIGNAL_LEVELS = {
        0x00: {"level": "No signal", "bars": 0},
        0x01: {"level": "Weak", "bars": 1},
        0x02: {"level": "Fair", "bars": 2},
        0x03: {"level": "Good", "bars": 3},
        0x04: {"level": "Strong", "bars": 4},
    }
    
    @staticmethod
    def calculate_crc(data: bytes) -> int:
        """Calculate CRC-ITU checksum"""
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x0001:
                    crc = (crc >> 1) ^ 0x8408
                else:
                    crc >>= 1
        return crc ^ 0xFFFF
    
    def parse_packet(self, packet: bytes) -> Optional[Dict[str, Any]]:
        """Parse a complete GPS tracker packet"""
        try:
            # Validate packet structure
            if len(packet) < 7:
                logger.warning(f"Packet too short: {len(packet)} bytes")
                return None
            
            if not packet.startswith(self.START_BIT):
                logger.warning("Invalid start bit")
                return None
            
            if not packet.endswith(self.STOP_BIT):
                logger.warning("Invalid stop bit")
                return None
            
            # Extract packet components
            length = packet[2]
            protocol_number = packet[3]
            
            # Verify length
            expected_length = length + 5  # +2 start, +1 length, +2 stop
            if len(packet) != expected_length:
                logger.warning(f"Length mismatch: expected {expected_length}, got {len(packet)}")
                return None
            
            # Verify CRC
            data_for_crc = packet[2:-4]  # From length to before CRC
            calculated_crc = self.calculate_crc(data_for_crc)
            packet_crc = struct.unpack('>H', packet[-4:-2])[0]
            
            if calculated_crc != packet_crc:
                logger.warning(f"CRC mismatch: calculated {calculated_crc:04X}, packet {packet_crc:04X}")
                # Don't return None, continue parsing - some devices have CRC issues
            
            # Parse based on protocol number
            if protocol_number == self.PACKET_LOGIN:
                return self.parse_login(packet)
            elif protocol_number == self.PACKET_LOCATION:
                return self.parse_location(packet)
            elif protocol_number == self.PACKET_HEARTBEAT:
                return self.parse_heartbeat(packet)
            elif protocol_number == self.PACKET_ALARM:
                return self.parse_alarm(packet)
            else:
                logger.warning(f"Unknown protocol number: 0x{protocol_number:02X}")
                return {
                    'type': 'unknown',
                    'protocol': protocol_number,
                    'raw_data': packet.hex()
                }
        
        except Exception as e:
            logger.error(f"Error parsing packet: {e}", exc_info=True)
            return None
    
    def parse_login(self, packet: bytes) -> Dict[str, Any]:
        """Parse login packet (0x01)"""
        try:
            # Login packet structure: 78 78 [length] 01 [IMEI 8 bytes] [info serial 2 bytes] [CRC 2 bytes] 0D 0A
            if len(packet) < 15:
                return None
            
            imei_bytes = packet[4:12]
            imei = ''.join(f'{b:02X}' for b in imei_bytes)
            
            serial_number = struct.unpack('>H', packet[12:14])[0]
            
            return {
                'type': 'login',
                'protocol': self.PACKET_LOGIN,
                'imei': imei,
                'serial_number': serial_number,
                'timestamp': datetime.utcnow()
            }
        
        except Exception as e:
            logger.error(f"Error parsing login packet: {e}")
            return None
    
    def parse_location(self, packet: bytes) -> Dict[str, Any]:
        """Parse location packet (0x12)"""
        try:
            # Location packet structure is complex, contains:
            # Date/time, GPS satellite count, latitude, longitude, speed, course/status
            
            if len(packet) < 30:
                return None
            
            idx = 4  # Start after header
            
            # Parse datetime (6 bytes: YY MM DD HH MM SS)
            year = 2000 + packet[idx]
            month = packet[idx + 1]
            day = packet[idx + 2]
            hour = packet[idx + 3]
            minute = packet[idx + 4]
            second = packet[idx + 5]
            idx += 6
            
            try:
                dt = datetime(year, month, day, hour, minute, second)
            except ValueError:
                dt = datetime.utcnow()
            
            # GPS info (1 byte: satellite count)
            gps_info = packet[idx]
            gps_length = gps_info & 0x0F
            satellite_count = (gps_info >> 4) & 0x0F
            idx += 1
            
            # Latitude (4 bytes)
            lat_raw = struct.unpack('>I', packet[idx:idx+4])[0]
            latitude = lat_raw / 1800000.0
            idx += 4
            
            # Longitude (4 bytes)
            lon_raw = struct.unpack('>I', packet[idx:idx+4])[0]
            longitude = lon_raw / 1800000.0
            idx += 4
            
            # Speed (1 byte, km/h)
            speed = packet[idx]
            idx += 1
            
            # Course/Status (2 bytes)
            # Bits 0-9: Course (0-360 degrees)
            # Bit 10: Longitude hemisphere (0=East, 1=West)
            # Bit 11: Latitude hemisphere (0=North, 1=South)
            # Bit 12: GPS positioned (1=valid, 0=invalid)
            # Bit 13-15: Reserved
            course_status = struct.unpack('>H', packet[idx:idx+2])[0]
            course = (course_status >> 6) & 0x03FF  # Bits 6-15 for course
            lon_hemisphere = (course_status >> 5) & 0x01  # Bit 5: 0=E, 1=W
            lat_hemisphere = (course_status >> 4) & 0x01  # Bit 4: 0=N, 1=S
            gps_real = (course_status >> 3) & 0x01  # Bit 3: GPS positioned
            idx += 2
            
            # Apply hemisphere corrections
            if lat_hemisphere == 1:  # South
                latitude = -latitude
            if lon_hemisphere == 1:  # West
                longitude = -longitude
            
            # LBS info (Mobile station info) - optional
            # MCC, MNC, LAC, Cell ID
            
            # Get serial number (2 bytes before CRC)
            serial_number = struct.unpack('>H', packet[-6:-4])[0]
            
            return {
                'type': 'location',
                'protocol': self.PACKET_LOCATION,
                'timestamp': dt,
                'latitude': latitude,
                'longitude': longitude,
                'speed': speed,
                'course': course,
                'satellites': satellite_count,
                'gps_valid': bool(gps_real),
                'serial_number': serial_number
            }
        
        except Exception as e:
            logger.error(f"Error parsing location packet: {e}", exc_info=True)
            return None
    
    def parse_heartbeat(self, packet: bytes) -> Dict[str, Any]:
        """Parse heartbeat packet (0x13)"""
        try:
            # Heartbeat structure: 78 78 [length] 13 [terminal info 1 byte] [voltage 2 bytes] 
            # [GSM signal 1 byte] [alarm 1 byte] [language 1 byte] [serial 2 bytes] [CRC] 0D 0A
            
            if len(packet) < 14:
                return None
            
            terminal_info = packet[4]
            voltage = struct.unpack('>H', packet[5:7])[0]
            gsm_signal = packet[7]
            alarm_info = packet[8]
            
            # Decode terminal info
            oil_electric_connected = bool(terminal_info & 0x01)
            gps_tracking = bool(terminal_info & 0x02)
            alarm_status = (terminal_info >> 2) & 0x03
            charging_status = (terminal_info >> 4) & 0x03
            acc_status = bool(terminal_info & 0x40)
            defense_activated = bool(terminal_info & 0x80)
            
            # Voltage level (0x00 - 0x06)
            voltage_level = (voltage >> 8) & 0xFF
            battery_info = self.BATTERY_LEVELS.get(voltage_level, {"level": "Unknown", "percent": 50})
            
            # GSM signal strength
            gsm_level = min(gsm_signal, 4)
            signal_info = self.GSM_SIGNAL_LEVELS.get(gsm_level, {"level": "Unknown", "bars": 0})
            
            serial_number = struct.unpack('>H', packet[-6:-4])[0]
            
            return {
                'type': 'heartbeat',
                'protocol': self.PACKET_HEARTBEAT,
                'timestamp': datetime.utcnow(),
                'voltage_level': voltage_level,
                'battery_percent': battery_info['percent'],
                'battery_status': battery_info['level'],
                'gsm_signal': gsm_signal,
                'signal_strength': signal_info['level'],
                'signal_bars': signal_info['bars'],
                'gps_tracking': gps_tracking,
                'acc_status': acc_status,
                'charging': charging_status > 0,
                'alarm_status': alarm_status,
                'serial_number': serial_number
            }
        
        except Exception as e:
            logger.error(f"Error parsing heartbeat packet: {e}")
            return None
    
    def parse_alarm(self, packet: bytes) -> Dict[str, Any]:
        """Parse alarm packet (0x16)"""
        try:
            # Alarm packet is similar to location packet but with alarm flags
            location_data = self.parse_location(packet)
            
            if location_data:
                location_data['type'] = 'alarm'
                location_data['protocol'] = self.PACKET_ALARM
                
                # Parse alarm type if available
                if len(packet) > 20:
                    alarm_type = packet[20]
                    alarm_names = {
                        0x00: "Normal",
                        0x01: "SOS",
                        0x02: "Power cut",
                        0x03: "Vibration",
                        0x04: "Enter fence",
                        0x05: "Exit fence",
                        0x06: "Over speed",
                        0x07: "Displacement"
                    }
                    location_data['alarm_type'] = alarm_names.get(alarm_type, f"Unknown (0x{alarm_type:02X})")
            
            return location_data
        
        except Exception as e:
            logger.error(f"Error parsing alarm packet: {e}")
            return None
    
    def create_response(self, packet_type: int, serial_number: int) -> bytes:
        """Create response packet for GPS tracker"""
        try:
            # Response format: 78 78 05 [protocol] [serial 2 bytes] [CRC 2 bytes] 0D 0A
            response_data = struct.pack('B', 5) + struct.pack('B', packet_type) + struct.pack('>H', serial_number)
            
            crc = self.calculate_crc(response_data)
            
            response = self.START_BIT + response_data + struct.pack('>H', crc) + self.STOP_BIT
            
            return response
        
        except Exception as e:
            logger.error(f"Error creating response: {e}")
            return None

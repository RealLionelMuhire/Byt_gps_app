"""
GPS Tracker Binary Protocol Parser

Parses binary packets from GPS trackers per the official protocol:
- Start 0x78 0x78, Stop 0x0D 0x0A
- Packet length = Protocol + Information Content + Serial Number + Error Check (5+N bytes)
- CRC-ITU from Packet Length through Information Serial Number (doc §4.6, Appendix A)
- Protocol numbers: 0x01 Login, 0x12 Location, 0x13 Heartbeat (status), 0x16 Alarm, 0x80 Command response
"""

import struct
from datetime import datetime
from typing import Optional, Dict, Any
import logging
from app.core.config import settings

logger = logging.getLogger(__name__)


class ProtocolParser:
    """Parse GPS tracker binary protocol packets"""
    
    # Packet types
    PACKET_LOGIN = 0x01
    PACKET_LOCATION = 0x12
    PACKET_HEARTBEAT = 0x13
    PACKET_STRING_INFO = 0x15
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
            elif protocol_number == self.PACKET_STRING_INFO:
                return self.parse_command_response(packet)
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
            
            # GPS info (Protocol doc 5.2.1.5: high nibble = length of GPS info, low nibble = satellite count; e.g. 0xCB = length 12, 11 satellites)
            gps_info = packet[idx]
            gps_length = (gps_info >> 4) & 0x0F
            satellite_count = gps_info & 0x0F
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
            
            # Correct bit extraction based on provided Protocol Document (Section 5.2.1.9)
            # BYTE_1 Bit 2 (Int Bit 10): Latitude (1=North, 0=South)
            # BYTE_1 Bit 3 (Int Bit 11): Longitude (0=East, 1=West)
            course = course_status & 0x03FF             # Bits 0-9 for course
            lat_bit = (course_status >> 10) & 0x01      # Bit 10: Lat
            lon_bit = (course_status >> 11) & 0x01      # Bit 11: Lon
            gps_real = (course_status >> 12) & 0x01     # Bit 12: GPS positioned
            
            idx += 2
            
            # Apply hemisphere corrections
            # Document says: Bit 2 is 1 for North. So 0 matches South.
            if lat_bit == 0:  # South
                latitude = -latitude
            elif settings.FORCE_SOUTHERN_HEMISPHERE and latitude > 0:
                # Still keep force option just in case, but the protocol fix should resolve it
                logger.warning(f"Forcing Southern Hemisphere (Config): {latitude} -> {-latitude}")
                latitude = -latitude
                
            # Document says: Bit 3 is 0 for East, 1 matches West (implied).
            if lon_bit == 1:  # West
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
        """Parse heartbeat packet (0x13, Protocol doc §5.4)"""
        try:
            # Heartbeat layout (doc §5.4.1):
            # [0-1] Start 0x7878
            # [2]   Packet Length
            # [3]   Protocol 0x13
            # [4]   Terminal Information  (1 byte)
            # [5]   Voltage Level         (1 byte, 0-6)
            # [6]   GSM Signal Strength   (1 byte, 0-4)
            # [7-8] Alarm/Language        (2 bytes: alarm, language)
            # [9-10] Serial Number        (2 bytes)
            # [11-12] CRC                 (2 bytes)
            # [13-14] Stop 0x0D0A
            # Total: 15 bytes
            
            if len(packet) < 15:
                return None
            
            terminal_info = packet[4]
            voltage_level = packet[5]
            gsm_signal = packet[6]
            alarm_info = packet[7]
            language = packet[8]
            
            # Decode terminal info (doc §5.4.1.4)
            # Bit 0: 1=Activated, 0=Deactivated
            # Bit 1: 1=ACC high, 0=ACC low
            # Bit 2: 1=Charge on, 0=Charge off
            # Bit 3-5: Alarm (000 Normal, 001 Shock, 010 Power cut, 011 Low battery, 100 SOS)
            # Bit 6: 1=GPS tracking on, 0=off
            # Bit 7: 1=Oil/electricity disconnected, 0=connected
            activated = bool(terminal_info & 0x01)
            acc_status = bool(terminal_info & 0x02)
            charging_status = bool(terminal_info & 0x04)
            alarm_status = (terminal_info >> 3) & 0x07
            gps_tracking = bool(terminal_info & 0x40)
            oil_elec_disconnected = bool(terminal_info & 0x80)
            
            # Voltage level (doc §5.4.1.5): 0-6
            battery_info = self.BATTERY_LEVELS.get(voltage_level, {"level": "Unknown", "percent": 50})
            
            # GSM signal (doc §5.4.1.6): 0x00-0x04
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
                'charging': charging_status,
                'alarm_status': alarm_status,
                'serial_number': serial_number
            }
        
        except Exception as e:
            logger.error(f"Error parsing heartbeat packet: {e}")
            return None
    
    def parse_alarm(self, packet: bytes) -> Dict[str, Any]:
        """Parse alarm packet (0x16, Protocol doc §5.3).
        
        Layout after protocol byte:
          Date(6) + GPS_info(1) + Lat(4) + Lon(4) + Speed(1) + Course(2) = 18 bytes of GPS (same as location)
          LBS_Length(1) + MCC(2) + MNC(1) + LAC(2) + Cell_ID(3)         = 9 bytes of LBS
          Terminal_Info(1) + Voltage(1) + GSM(1) + Alarm(1) + Language(1) = 5 bytes of status
          Serial(2) + CRC(2)                                             = 4 bytes
        Alarm byte is at offset 4 + 18 + 9 + 3 = 34 from packet start (after Terminal, Voltage, GSM).
        But LBS_Length can vary; use it to compute offset dynamically.
        """
        try:
            location_data = self.parse_location(packet)
            
            if location_data:
                location_data['type'] = 'alarm'
                location_data['protocol'] = self.PACKET_ALARM
                
                # After GPS fields (18 bytes from offset 4), there's the LBS block.
                # LBS Length byte is at offset 22 (= 4 + 18); its value includes itself
                # (e.g. 0x09 = 1 byte length + 8 bytes MCC/MNC/LAC/Cell).
                # Status fields start at: 22 + lbs_length
                if len(packet) > 23:
                    lbs_length = packet[22]
                    status_offset = 22 + lbs_length
                    # Status: Terminal_Info(1), Voltage(1), GSM(1), then Alarm/Language(2)
                    alarm_offset = status_offset + 3  # skip Terminal_Info, Voltage, GSM
                    
                    if len(packet) > alarm_offset:
                        alarm_byte = packet[alarm_offset]
                        alarm_names = {
                            0x00: "Normal",
                            0x01: "SOS",
                            0x02: "Power cut",
                            0x03: "Shock",
                            0x04: "Enter fence",
                            0x05: "Exit fence",
                            0x06: "Over speed",
                            0x07: "Ignition on",
                            0x08: "Ignition off",
                            0x09: "AC on",
                            0x0A: "AC off",
                        }
                        location_data['alarm_type'] = alarm_names.get(alarm_byte, f"Unknown (0x{alarm_byte:02X})")
                    
                        # Also extract terminal info for ACC, battery, etc.
                        terminal_info = packet[status_offset]
                        location_data['acc_status'] = bool(terminal_info & 0x02)
                        location_data['gps_tracking'] = bool(terminal_info & 0x40)
                        
                        voltage_level = packet[status_offset + 1]
                        battery_info = self.BATTERY_LEVELS.get(voltage_level, {"level": "Unknown", "percent": 50})
                        location_data['battery_percent'] = battery_info['percent']
                        
                        gsm_signal = packet[status_offset + 2]
                        signal_info = self.GSM_SIGNAL_LEVELS.get(min(gsm_signal, 4), {"level": "Unknown", "bars": 0})
                        location_data['signal_bars'] = signal_info['bars']
            
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
    
    def create_command_packet(self, command: str, serial_number: int,
                              server_flag: int = 1) -> bytes:
        """Build a Protocol 0x80 command packet to send to the terminal (doc §6.1).

        Verified against Appendix B (DYD example). The server→device packet has
        no Language field (the terminal's 0x15 reply does include one).

        Layout:
          Start(2) + Length(1) + Protocol 0x80(1) + CmdLength(1) + ServerFlag(4)
          + CommandContent(M) + Serial(2) + CRC(2) + Stop(2)

        CmdLength = 4 + M  (ServerFlag + Command bytes)
        PacketLength = 1(protocol) + 1(cmdlen) + 4(flag) + M + 2(serial) + 2(crc) = 10 + M
        """
        try:
            cmd_bytes = command.encode('ascii')
            cmd_length = 4 + len(cmd_bytes)
            packet_length = 10 + len(cmd_bytes)

            data = bytearray()
            data.append(packet_length)                          # Packet Length
            data.append(self.PACKET_COMMAND)                    # Protocol 0x80
            data.append(cmd_length)                             # Length of Command
            data.extend(struct.pack('>I', server_flag))         # Server Flag (4 bytes)
            data.extend(cmd_bytes)                              # Command Content (ASCII)
            data.extend(struct.pack('>H', serial_number))       # Serial Number

            crc = self.calculate_crc(bytes(data))

            packet = self.START_BIT + bytes(data) + struct.pack('>H', crc) + self.STOP_BIT
            return packet

        except Exception as e:
            logger.error(f"Error creating command packet: {e}")
            return None
    
    def parse_command_response(self, packet: bytes) -> Optional[Dict[str, Any]]:
        """Parse a 0x15 command response from the terminal (doc §6.2).

        Layout (same as server command but protocol = 0x15):
          Start(2) + Length(1) + Protocol 0x15(1) + CmdLength(1) + ServerFlag(4)
          + CommandContent(M) + Language(2) + Serial(2) + CRC(2) + Stop(2)
        """
        try:
            if len(packet) < 17:
                return None

            cmd_length = packet[4]
            server_flag = struct.unpack('>I', packet[5:9])[0]
            content_length = cmd_length - 4
            if content_length < 0 or len(packet) < 9 + content_length:
                return None

            command_content = packet[9:9 + content_length].decode('ascii', errors='replace')
            serial_number = struct.unpack('>H', packet[-6:-4])[0]

            return {
                'type': 'command_response',
                'protocol': self.PACKET_STRING_INFO,
                'server_flag': server_flag,
                'content': command_content,
                'serial_number': serial_number,
                'timestamp': datetime.utcnow()
            }

        except Exception as e:
            logger.error(f"Error parsing command response: {e}")
            return None

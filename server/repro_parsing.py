
import sys
from unittest.mock import MagicMock

# Mock pydantic_settings to avoid ImportError
sys.modules["pydantic_settings"] = MagicMock()
sys.modules["pydantic_settings"].BaseSettings = object

# Mock app.core.config
config_mock = MagicMock()
config_mock.settings.FORCE_SOUTHERN_HEMISPHERE = False
config_mock.settings.LOG_LEVEL = "INFO"
sys.modules["app.core.config"] = config_mock

import struct

from app.protocol_parser import ProtocolParser

def test_parsing():
    parser = ProtocolParser()
    
    # Construct a valid packet with known Course/Status
    # we need 30+ bytes
    # Index 22-23 is Course/Status (after lat/lon/speed)
    
    # Let's craft the payload after the header (4 bytes: 78 78 len prot)
    # DateTime: 6 bytes
    # Sat: 1 byte
    # Lat: 4 bytes
    # Lon: 4 bytes
    # Speed: 1 byte
    # Course/Status: 2 bytes
    
    # Values
    dt = b'\x14\x01\x16\x12\x00\x00' # 2020-01-22 ...
    sat = b'\x05' # 5 sats
    lat = struct.pack('>I', 1800000) # 1.0 degrees
    lon = struct.pack('>I', 1800000) # 1.0 degrees
    speed = b'\x00'
    
    # Scenario 1: Course=0.
    # We want to test South.
    # Protocol says: Byte 1 Bit 2 (Int Bit 10) is Latitude. 0 = South, 1 = North.
    # Byte 1 Bit 3 (Int Bit 11) is Longitude. 0 = East, 1 = West.
    
    # To represent South East:
    # Bit 10 (Lat) = 0.
    # Bit 11 (Lon) = 0.
    # Bit 12 (GPS) = 1 (Valid).
    # Value: 0x1000 (Bit 12 set, Bits 10,11 zero).
    course_status_south_east = struct.pack('>H', 0x1000)
    
    # Padding for LBS (8 bytes)
    padding = b'\x00' * 8
    
    # Length calculation: 36 total - 5 = 31
    length_val = 31
    length_byte = struct.pack('B', length_val)
    
    # CRC Input: Length + Prot + Data + LBS + Serial
    crc_input = length_byte + b'\x12' + dt + sat + lat + lon + speed + course_status_south_east + padding + b'\x00\x01'
    crc = parser.calculate_crc(crc_input)
    
    packet = b'\x78\x78' + crc_input + struct.pack('>H', crc) + b'\x0D\x0A'
    
    print(f"Testing Packet with CourseStatus=0x1000 (Bits 10,11=0 -> South, East)")
    result = parser.parse_packet(packet)
    
    if result and result['type'] == 'location':
        print(f"Latitude: {result['latitude']}")
        print(f"Longitude: {result['longitude']}")
        
        if result['latitude'] < 0:
            print("SUCCESS: Latitude is Negative (South)")
        else:
            print("FAILURE: Latitude is Positive (North)")
            
        if result['longitude'] > 0:
             print("SUCCESS: Longitude is Positive (East)")
        else:
             print("FAILURE: Longitude is Negative (West)")
    else:
        print("Failed to parse packet")

    # Scenario 2: North West
    # Bit 10 (Lat) = 1 (North).
    # Bit 11 (Lon) = 1 (West).
    # Bit 12 (GPS) = 1.
    # Value: 0x1C00 (Bits 12, 11, 10 set).
    
    course_status_north_west = struct.pack('>H', 0x1C00)
    
    # Reconstruct packet 2
    crc_input2 = length_byte + b'\x12' + dt + sat + lat + lon + speed + course_status_north_west + padding + b'\x00\x02'
    crc2 = parser.calculate_crc(crc_input2)
    packet2 = b'\x78\x78' + crc_input2 + struct.pack('>H', crc2) + b'\x0D\x0A'
    
    print(f"\nTesting Packet with CourseStatus=0x1C00 (Bits 10,11=1 -> North, West)")
    result2 = parser.parse_packet(packet2)
    
    if result2 and result2['type'] == 'location':
        print(f"Latitude: {result2['latitude']}")
        print(f"Longitude: {result2['longitude']}")
        
        if result2['latitude'] > 0:
            print("SUCCESS: Latitude is Positive (North)")
        else:
            print("FAILURE: Latitude is Negative (South)")
            
        if result2['longitude'] < 0:
            print("SUCCESS: Longitude is Negative (West)")
        else:
            print("FAILURE: Longitude is Positive (East)")

if __name__ == "__main__":
    test_parsing()

if __name__ == "__main__":
    test_parsing()

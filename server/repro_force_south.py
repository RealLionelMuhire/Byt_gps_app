import sys
from unittest.mock import MagicMock

# Mock pydantic_settings to avoid ImportError
sys.modules["pydantic_settings"] = MagicMock()
sys.modules["pydantic_settings"].BaseSettings = object

# Mock app.core.config
config_mock = MagicMock()
config_mock.settings.FORCE_SOUTHERN_HEMISPHERE = True
sys.modules["app.core.config"] = config_mock

import struct
from app.protocol_parser import ProtocolParser
# settings is already mocked in sys.modules["app.core.config"]
settings = config_mock.settings

def test_force_south():
    print(f"Force South Setting: {settings.FORCE_SOUTHERN_HEMISPHERE}")
    parser = ProtocolParser()
    
    # Values
    dt = b'\x14\x01\x16\x12\x00\x00' 
    sat = b'\x05'
    lat = struct.pack('>I', 3600000) # 2.0 degrees
    lon = struct.pack('>I', 3600000) # 2.0 degrees
    speed = b'\x00'
    
    # Course/Status: Course=0, North (Bit 11=0)
    # Bits: 0000 0000 0000 0000
    course_status_north = struct.pack('>H', 0x0000) # Explicitly North
    
    padding = b'\x00' * 8
    length_byte = struct.pack('B', 31)
    
    # Construct Packet
    crc_input = length_byte + b'\x12' + dt + sat + lat + lon + speed + course_status_north + padding + b'\x00\x01'
    crc = parser.calculate_crc(crc_input)
    packet = b'\x78\x78' + crc_input + struct.pack('>H', crc) + b'\x0D\x0A'
    
    print(f"Testing Packet with North Flag (Bit 11=0)...")
    result = parser.parse_packet(packet)
    
    if result and result['type'] == 'location':
        print(f"Latitude: {result['latitude']}")
        if result['latitude'] < 0:
            print("SUCCESS: Latitude forced to Negative (South)")
        else:
            print("FAILURE: Latitude stayed Positive (North)")
    else:
        print("Failed to parse packet")

if __name__ == "__main__":
    test_force_south()

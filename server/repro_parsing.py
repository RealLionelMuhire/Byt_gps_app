
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
    
    # Scenario 1: Course=0, Status: South (Bit 11=1), East (Bit 10=0)
    # Bit 11 is 1. 0x0800.
    # Course is 0.
    # Total = 0x0800.
    course_status_south = struct.pack('>H', 0x0800)
    
    # Padding for LBS (8 bytes) to satisfy len >= 30 check
    padding = b'\x00' * 8
    
    # Full packet construction
    # 78 78 [Len] 12 [Dt] [Sat] [Lat] [Lon] [Speed] [Course] [LBS] [Serial] [CRC] 0D 0A
    # Data = 18 bytes. LBS = 8 bytes.
    # Content for CRC = [Len] + [Prot] + [Data] + [LBS] + [Serial]
    # Length byte value = Prot(1) + Data(18) + LBS(8) + Serial(2) + CRC(2)? No, usually Length is (PacketLen - 4) or similar.
    # Code says: expected_length = length + 5.
    # So Length = Total - 5.
    # Total components: Header(2) + Len(1) + Prot(1) + Data(18) + LBS(8) + Serial(2) + CRC(2) + Stop(2) = 36 bytes.
    # Length Value = 36 - 5 = 31 (0x1F).
    
    length_val = 31
    length_byte = struct.pack('B', length_val)
    
    # CRC Input: Length + Prot + Data + LBS + Serial
    crc_input = length_byte + b'\x12' + dt + sat + lat + lon + speed + course_status_south + padding + b'\x00\x01'
    crc = parser.calculate_crc(crc_input)
    
    packet = b'\x78\x78' + crc_input + struct.pack('>H', crc) + b'\x0D\x0A'
    
    print(f"Testing Packet with CourseStatus=0x0800 (South=True, Course=0)")
    result = parser.parse_packet(packet)
    
    if result and result['type'] == 'location':
        print(f"Latitude: {result['latitude']}")
        if result['latitude'] < 0:
            print("SUCCESS: Latitude is Negative (South)")
        else:
            print("FAILURE: Latitude is Positive (North)")
    else:
        print("Failed to parse packet")

    # Scenario 2: Course 16 (Bit 4 set), North (Bit 11=0)
    # Course = 16 (0x0010). Status = 0.
    # Total = 0x0010.
    # Should be North.
    # Current code: reads Bit 4 (1) -> thinks it is South.
    
    course_status_fake_south = struct.pack('>H', 0x0010)
    
    # Reconstruct packet 2
    crc_input2 = length_byte + b'\x12' + dt + sat + lat + lon + speed + course_status_fake_south + padding + b'\x00\x02'
    crc2 = parser.calculate_crc(crc_input2)
    packet2 = b'\x78\x78' + crc_input2 + struct.pack('>H', crc2) + b'\x0D\x0A'
    
    print(f"\nTesting Packet with CourseStatus=0x0010 (South=False, Course=16; Bit 4 is 1)")
    result2 = parser.parse_packet(packet2)
    
    if result2 and result2['type'] == 'location':
        print(f"Latitude: {result2['latitude']}")
        if result2['latitude'] > 0:
            print("SUCCESS: Latitude is Positive (North)")
        else:
            print("FAILURE: Latitude is Negative (South) - False Positive from Course Bits")

if __name__ == "__main__":
    test_parsing()

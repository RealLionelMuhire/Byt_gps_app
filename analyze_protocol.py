#!/usr/bin/env python3
"""
Analyze GPS Tracker Protocol
Captures raw data and analyzes patterns to determine actual protocol
"""

import serial
import time
import sys
from collections import Counter

def try_baudrate(port, baudrate, duration=10):
    """Try a specific baud rate and collect data"""
    print(f"\n{'='*60}")
    print(f"Testing baud rate: {baudrate}")
    print(f"{'='*60}")
    
    try:
        ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1
        )
        
        print(f"✅ Connected at {baudrate} baud")
        print(f"Collecting data for {duration} seconds...")
        
        all_data = bytearray()
        start_time = time.time()
        
        while (time.time() - start_time) < duration:
            if ser.in_waiting > 0:
                data = ser.read(ser.in_waiting)
                all_data.extend(data)
                print(f"  Received {len(data)} bytes (total: {len(all_data)})")
            time.sleep(0.1)
        
        ser.close()
        
        if len(all_data) == 0:
            print("❌ No data received")
            return None
        
        print(f"\n📊 Analysis for {baudrate} baud:")
        print(f"   Total bytes: {len(all_data)}")
        
        # Analyze byte patterns
        analyze_data(all_data)
        
        return all_data
        
    except serial.SerialException as e:
        print(f"❌ Error: {e}")
        return None

def analyze_data(data):
    """Analyze data for patterns"""
    
    # 1. Look for common start patterns
    print("\n🔍 Looking for potential start patterns:")
    start_patterns = {}
    for i in range(len(data) - 1):
        pattern = (data[i], data[i+1])
        start_patterns[pattern] = start_patterns.get(pattern, 0) + 1
    
    # Show top 10 most common 2-byte patterns
    sorted_patterns = sorted(start_patterns.items(), key=lambda x: x[1], reverse=True)[:10]
    for pattern, count in sorted_patterns:
        print(f"   {pattern[0]:02X} {pattern[1]:02X} - appears {count} times")
    
    # 2. Look for protocol markers
    print("\n🔍 Looking for protocol markers:")
    markers = {
        '0x7878': (0x78, 0x78),  # Expected start
        '0x0D0A': (0x0D, 0x0A),  # Expected end
        '0x0101': (0x01, 0x01),  # Login
        '0x1212': (0x12, 0x12),  # Location
        '0x1313': (0x13, 0x13),  # Heartbeat
        '0x1616': (0x16, 0x16),  # Alarm
    }
    
    for name, (b1, b2) in markers.items():
        count = 0
        positions = []
        for i in range(len(data) - 1):
            if data[i] == b1 and data[i+1] == b2:
                count += 1
                if len(positions) < 5:
                    positions.append(i)
        
        if count > 0:
            print(f"   {name}: found {count} times at positions {positions}")
    
    # 3. Byte frequency analysis
    print("\n📊 Most common bytes:")
    byte_freq = Counter(data)
    for byte_val, count in byte_freq.most_common(15):
        percent = (count / len(data)) * 100
        print(f"   0x{byte_val:02X} ({chr(byte_val) if 32 <= byte_val < 127 else '?'}): {count} times ({percent:.1f}%)")
    
    # 4. Look for ASCII vs binary
    ascii_count = sum(1 for b in data if 32 <= b < 127)
    ascii_percent = (ascii_count / len(data)) * 100
    print(f"\n📝 ASCII characters: {ascii_count}/{len(data)} ({ascii_percent:.1f}%)")
    
    if ascii_percent > 50:
        print("   → Looks like ASCII/text data")
        print("\n   Sample (first 200 bytes as text):")
        try:
            print(f"   {data[:200].decode('ascii', errors='replace')}")
        except:
            pass
    else:
        print("   → Looks like binary data")
    
    # 5. Show hex dump of first 200 bytes
    print("\n📦 First 200 bytes (hex):")
    for i in range(0, min(200, len(data)), 16):
        hex_part = ' '.join(f'{b:02X}' for b in data[i:i+16])
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data[i:i+16])
        print(f"   {i:04X}: {hex_part:<48} {ascii_part}")
    
    # 6. Look for repeating sequences
    print("\n🔄 Looking for repeating sequences (4+ bytes):")
    sequences = {}
    for length in [4, 5, 6, 7, 8]:
        for i in range(len(data) - length):
            seq = tuple(data[i:i+length])
            if seq not in sequences:
                # Count occurrences
                count = 0
                for j in range(len(data) - length):
                    if tuple(data[j:j+length]) == seq:
                        count += 1
                if count >= 3:
                    sequences[seq] = count
    
    # Show top repeating sequences
    sorted_seqs = sorted(sequences.items(), key=lambda x: x[1], reverse=True)[:5]
    for seq, count in sorted_seqs:
        hex_seq = ' '.join(f'{b:02X}' for b in seq)
        print(f"   [{hex_seq}] - appears {count} times")

def main():
    port = '/dev/ttyUSB0'
    
    print("🔍 GPS Tracker Protocol Analyzer")
    print("=" * 60)
    print(f"Port: {port}")
    print("This will test multiple baud rates and analyze the data")
    print("=" * 60)
    
    # Test common baud rates
    baudrates = [9600, 19200, 38400, 57600, 115200]
    results = {}
    
    for baudrate in baudrates:
        data = try_baudrate(port, baudrate, duration=15)
        if data:
            results[baudrate] = data
        
        print("\nWaiting 2 seconds before next test...")
        time.sleep(2)
    
    # Summary
    print("\n" + "=" * 60)
    print("📈 SUMMARY")
    print("=" * 60)
    
    if not results:
        print("❌ No data received at any baud rate")
        return
    
    for baudrate, data in results.items():
        print(f"\n{baudrate} baud: {len(data)} bytes received")
        
        # Check for protocol markers
        has_7878 = data.find(b'\x78\x78') != -1
        has_0d0a = data.find(b'\x0d\x0a') != -1
        ascii_percent = (sum(1 for b in data if 32 <= b < 127) / len(data)) * 100
        
        print(f"   - 0x7878 marker: {'✅ Found' if has_7878 else '❌ Not found'}")
        print(f"   - 0x0D0A marker: {'✅ Found' if has_0d0a else '❌ Not found'}")
        print(f"   - ASCII content: {ascii_percent:.1f}%")
        
        if has_7878 and has_0d0a:
            print(f"   → ✅ LIKELY CORRECT BAUD RATE")
    
    # Save best result to file
    best_baudrate = max(results.keys(), key=lambda k: len(results[k]))
    print(f"\n💾 Saving data from {best_baudrate} baud to 'captured_data.bin'")
    with open('captured_data.bin', 'wb') as f:
        f.write(results[best_baudrate])
    print("   You can analyze this file with: hexdump -C captured_data.bin")

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Analysis stopped by user")
        sys.exit(0)

#!/usr/bin/env python3
"""
GPS Tracker Configuration Tool for Ubuntu
Supports G06L/G07L and C32 models via AT commands
"""

import serial
import time
import sys

class GPSConfigurator:
    def __init__(self, port='/dev/ttyUSB0', baudrate=115200, timeout=2):
        """Initialize serial connection to GPS tracker"""
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None
        
    def connect(self):
        """Open serial connection"""
        try:
            self.ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE
            )
            print(f"✓ Connected to {self.port} at {self.baudrate} baud")
            time.sleep(0.5)  # Wait for connection to stabilize
            return True
        except serial.SerialException as e:
            print(f"✗ Failed to connect: {e}")
            print(f"  Make sure you run with: sudo python3 {sys.argv[0]}")
            return False
    
    def send_command(self, command, wait_response=True):
        """Send AT command to GPS tracker"""
        if not self.ser or not self.ser.is_open:
            print("✗ Serial port not open")
            return None
        
        # Ensure command ends with newline
        if not command.endswith('\r\n'):
            command = command + '\r\n'
        
        print(f"\n→ Sending: {command.strip()}")
        
        try:
            # Clear input buffer
            self.ser.reset_input_buffer()
            
            # Send command
            self.ser.write(command.encode('utf-8'))
            self.ser.flush()
            
            if wait_response:
                time.sleep(0.5)  # Wait for device to process
                
                # Read response
                response = b''
                start_time = time.time()
                while (time.time() - start_time) < self.timeout:
                    if self.ser.in_waiting > 0:
                        chunk = self.ser.read(self.ser.in_waiting)
                        response += chunk
                        time.sleep(0.1)  # Small delay for more data
                    else:
                        if response:  # If we got some data, wait a bit more
                            time.sleep(0.2)
                            if self.ser.in_waiting == 0:
                                break
                
                if response:
                    decoded = response.decode('utf-8', errors='ignore').strip()
                    print(f"← Response: {decoded}")
                    return decoded
                else:
                    print("← No response received")
                    return None
        except Exception as e:
            print(f"✗ Error: {e}")
            return None
    
    def configure_g06l(self, password, server_ip, server_port, apn='', apn_user='', apn_pass='', admin_phone=''):
        """Configure G06L/G07L model"""
        print("\n" + "="*60)
        print("Configuring G06L/G07L GPS Tracker")
        print("="*60)
        
        commands = []
        
        # Set server IP and port
        commands.append(f"AT+ZDR=adminip{password} {server_ip} {server_port}")
        
        # Set APN if provided
        if apn:
            commands.append(f"AT+ZDR=apn{password} {apn} {apn_user} {apn_pass}")
        
        # Set upload interval: 20s when moving, 60 seconds when stopped (060m = 60 sec on this device)
        commands.append(f"AT+ZDR=fix020s060m***n{password}")
        
        # Set admin phone if provided
        if admin_phone:
            commands.append(f"AT+ZDR=admin{password} {admin_phone}")
        
        # Check configuration
        commands.append(f"AT+ZDR=check{password}")
        commands.append(f"AT+ZDR=IPAPN{password}")
        
        # Execute commands
        for cmd in commands:
            self.send_command(cmd)
            time.sleep(1)  # Wait between commands
        
        print("\n✓ Configuration completed!")
        print("  You may want to reset the device with: AT+ZDR=reset{password}")
    
    def configure_c32(self, password, server_ip, server_port, apn='', apn_user='', apn_pass=''):
        """Configure C32 model"""
        print("\n" + "="*60)
        print("Configuring C32 GPS Tracker")
        print("="*60)
        
        commands = []
        
        # Set server IP and port
        commands.append(f"AT+ZDR=SERVER,1,{server_ip},{server_port},0#")
        
        # Set APN if provided
        if apn:
            commands.append(f"AT+ZDR=APN,{apn},{apn_user},{apn_pass}#")
        
        # Check configuration
        commands.append(f"AT+ZDR=Status#")
        commands.append(f"AT+ZDR=IP#")
        
        # Execute commands
        for cmd in commands:
            self.send_command(cmd)
            time.sleep(1)
        
        print("\n✓ Configuration completed!")
    
    def interactive_mode(self):
        """Interactive command mode"""
        print("\n" + "="*60)
        print("Interactive Mode - Type AT commands directly")
        print("="*60)
        print("Commands will be sent as-is. Type 'exit' to quit.\n")
        
        while True:
            try:
                cmd = input("AT> ").strip()
                if cmd.lower() in ['exit', 'quit', 'q']:
                    break
                if cmd:
                    self.send_command(cmd)
            except KeyboardInterrupt:
                print("\n\nExiting...")
                break
    
    def close(self):
        """Close serial connection"""
        if self.ser and self.ser.is_open:
            self.ser.close()
            print("\n✓ Connection closed")


def main():
    print("="*60)
    print("GPS Tracker Configuration Tool")
    print("="*60)
    
    # Check if running with sudo
    import os
    if os.geteuid() != 0:
        print("\n⚠ Warning: You may need to run with sudo:")
        print(f"  sudo python3 {sys.argv[0]}")
        print()
    
    # Initialize configurator
    gps = GPSConfigurator(port='/dev/ttyUSB0', baudrate=115200)
    
    if not gps.connect():
        return
    
    try:
        # Menu
        print("\nSelect mode:")
        print("1. Configure G06L/G07L model")
        print("2. Configure C32 model")
        print("3. Interactive mode (send custom commands)")
        print("4. Quick test (send check command)")
        
        choice = input("\nEnter choice (1-4): ").strip()
        
        if choice == '1':
            print("\n--- G06L/G07L Configuration ---")
            password = input("Password (default: 123456): ").strip() or "123456"
            server_ip = input("Server IP/Domain: ").strip()
            server_port = input("Server Port: ").strip()
            apn = input("APN (optional, press Enter to skip): ").strip()
            apn_user = input("APN Username (optional): ").strip() if apn else ""
            apn_pass = input("APN Password (optional): ").strip() if apn else ""
            admin_phone = input("Admin Phone Number (optional): ").strip()
            
            gps.configure_g06l(password, server_ip, server_port, apn, apn_user, apn_pass, admin_phone)
            
        elif choice == '2':
            print("\n--- C32 Configuration ---")
            password = input("Password (default: 123456): ").strip() or "123456"
            server_ip = input("Server IP/Domain: ").strip()
            server_port = input("Server Port: ").strip()
            apn = input("APN (optional): ").strip()
            apn_user = input("APN Username (optional): ").strip() if apn else ""
            apn_pass = input("APN Password (optional): ").strip() if apn else ""
            
            gps.configure_c32(password, server_ip, server_port, apn, apn_user, apn_pass)
            
        elif choice == '3':
            gps.interactive_mode()
            
        elif choice == '4':
            print("\n--- Quick Test ---")
            password = input("Password (default: 123456): ").strip() or "123456"
            gps.send_command(f"AT+ZDR=check{password}")
            time.sleep(1)
            gps.send_command(f"AT+ZDR=IPAPN{password}")
            
        else:
            print("Invalid choice")
    
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    finally:
        gps.close()


if __name__ == "__main__":
    main()

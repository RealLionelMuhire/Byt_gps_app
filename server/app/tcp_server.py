"""
TCP Server for GPS Trackers
Listens on port 7018 for incoming GPS tracker connections
Handles binary protocol communication
"""

import asyncio
import logging
from typing import Dict, Set
from datetime import datetime
import struct

from app.protocol_parser import ProtocolParser
from app.core.database import SessionLocal
from app.models.device import Device
from app.models.location import Location

logger = logging.getLogger(__name__)


class GPSTrackerConnection:
    """Represents a single GPS tracker connection"""
    
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, 
                 address: tuple, server: 'TCPServer'):
        self.reader = reader
        self.writer = writer
        self.address = address
        self.server = server
        self.parser = ProtocolParser()
        self.device_imei = None
        self.authenticated = False
        self.buffer = bytearray()
        self.last_activity = datetime.utcnow()
        self._command_serial = 0xA000
        self._pending_response: asyncio.Future = None
        
        logger.info(f"New connection from {address}")
    
    async def handle(self):
        """Handle GPS tracker connection"""
        try:
            while True:
                # Read data from tracker
                data = await self.reader.read(1024)
                
                if not data:
                    logger.info(f"Connection closed by {self.address}")
                    break
                
                self.last_activity = datetime.utcnow()
                self.buffer.extend(data)
                
                # Process complete packets in buffer
                await self.process_buffer()
        
        except asyncio.CancelledError:
            logger.info(f"Connection cancelled for {self.address}")
        except Exception as e:
            logger.error(f"Error handling connection from {self.address}: {e}", exc_info=True)
        finally:
            await self.close()
    
    async def process_buffer(self):
        """Process packets in the buffer"""
        while len(self.buffer) >= 7:  # Minimum packet size
            # Look for start bit
            start_index = self.buffer.find(self.parser.START_BIT)
            
            if start_index == -1:
                # No start bit found, keep last byte
                if len(self.buffer) > 1:
                    self.buffer = self.buffer[-1:]
                break
            
            # Remove data before start bit
            if start_index > 0:
                logger.debug(f"Discarded {start_index} bytes before start bit")
                self.buffer = self.buffer[start_index:]
            
            # Check if we have enough data to read packet length
            if len(self.buffer) < 3:
                break
            
            # Read packet length
            packet_length = self.buffer[2]
            total_length = packet_length + 5  # +2 start, +1 length, +2 stop
            
            # Check if we have complete packet
            if len(self.buffer) < total_length:
                logger.debug(f"Waiting for more data: have {len(self.buffer)}, need {total_length}")
                break
            
            # Extract complete packet
            packet = bytes(self.buffer[:total_length])
            self.buffer = self.buffer[total_length:]
            
            # Parse and handle packet
            await self.handle_packet(packet)
    
    async def handle_packet(self, packet: bytes):
        """Parse and handle a single packet"""
        try:
            logger.debug(f"Received packet ({len(packet)} bytes): {packet.hex()}")
            
            # Parse packet
            parsed = self.parser.parse_packet(packet)
            
            if not parsed:
                logger.warning(f"Failed to parse packet: {packet.hex()}")
                return
            
            logger.info(f"Parsed packet type: {parsed['type']} from {self.address}")
            
            # Handle based on packet type
            if parsed['type'] == 'login':
                await self.handle_login(parsed)
            elif parsed['type'] == 'location':
                await self.handle_location(parsed)
            elif parsed['type'] == 'heartbeat':
                await self.handle_heartbeat(parsed)
            elif parsed['type'] == 'alarm':
                await self.handle_alarm(parsed)
            elif parsed['type'] == 'command_response':
                await self.handle_command_response(parsed)
                return
            
            # Send response (not for command_response — terminal doesn't expect one)
            response = self.parser.create_response(parsed['protocol'], parsed.get('serial_number', 0))
            if response:
                await self.send_data(response)
        
        except Exception as e:
            logger.error(f"Error handling packet: {e}", exc_info=True)
    
    async def handle_login(self, data: Dict):
        """Handle login packet"""
        try:
            imei = data['imei']
            self.device_imei = imei
            self.authenticated = True
            
            logger.info(f"Device logged in: IMEI {imei}")
            
            # Store/update device in database
            db = SessionLocal()
            try:
                device = db.query(Device).filter(Device.imei == imei).first()
                
                if not device:
                    # Create new device
                    device = Device(
                        imei=imei,
                        name=f"Tracker-{imei[-6:]}",
                        status='online',
                        last_connect=datetime.utcnow()
                    )
                    db.add(device)
                    logger.info(f"Created new device: {imei}")
                else:
                    # Update existing device
                    device.status = 'online'
                    device.last_connect = datetime.utcnow()
                    logger.info(f"Updated device: {imei}")
                
                db.commit()
                
                # Register connection in server
                self.server.register_device(imei, self)
            
            finally:
                db.close()
        
        except Exception as e:
            logger.error(f"Error handling login: {e}", exc_info=True)
    
    async def handle_location(self, data: Dict):
        """Handle location packet"""
        try:
            if not self.authenticated or not self.device_imei:
                logger.warning("Location data from unauthenticated device")
                return
            
            logger.info(f"Location update from {self.device_imei}: "
                       f"({data['latitude']:.6f}, {data['longitude']:.6f}), "
                       f"speed: {data['speed']}km/h, satellites: {data['satellites']}")
            
            # Store location in database
            db = SessionLocal()
            try:
                device = db.query(Device).filter(Device.imei == self.device_imei).first()
                
                if device:
                    # Create location record
                    location = Location(
                        device_id=device.id,
                        latitude=data['latitude'],
                        longitude=data['longitude'],
                        speed=data['speed'],
                        course=data['course'],
                        satellites=data['satellites'],
                        gps_valid=data['gps_valid'],
                        timestamp=data['timestamp']
                    )
                    db.add(location)
                    
                    # Update device's last known location
                    device.last_latitude = data['latitude']
                    device.last_longitude = data['longitude']
                    device.last_update = datetime.utcnow()
                    device.status = 'online'
                    
                    db.commit()
                    
                    # Broadcast to WebSocket clients
                    await self.server.broadcast_location_update(device.id, data)
            
            finally:
                db.close()
        
        except Exception as e:
            logger.error(f"Error handling location: {e}", exc_info=True)
    
    async def handle_heartbeat(self, data: Dict):
        """Handle heartbeat packet"""
        try:
            if not self.authenticated or not self.device_imei:
                logger.warning("Heartbeat from unauthenticated device")
                return
            
            logger.info(f"Heartbeat from {self.device_imei}: "
                       f"Battery {data['battery_percent']}%, "
                       f"Signal: {data['signal_bars']}/4 bars")
            
            # Update device status in database
            db = SessionLocal()
            try:
                device = db.query(Device).filter(Device.imei == self.device_imei).first()
                
                if device:
                    device.battery_level = data['battery_percent']
                    device.gsm_signal = data['gsm_signal']
                    device.status = 'online'
                    device.last_update = datetime.utcnow()
                    db.commit()
            
            finally:
                db.close()
        
        except Exception as e:
            logger.error(f"Error handling heartbeat: {e}", exc_info=True)
    
    async def handle_alarm(self, data: Dict):
        """Handle alarm packet"""
        try:
            if not self.authenticated or not self.device_imei:
                logger.warning("Alarm from unauthenticated device")
                return
            
            alarm_type = data.get('alarm_type', 'Unknown')
            logger.warning(f"ALARM from {self.device_imei}: {alarm_type} "
                          f"at ({data['latitude']:.6f}, {data['longitude']:.6f})")
            
            # Store alarm as location with alarm flag
            db = SessionLocal()
            try:
                device = db.query(Device).filter(Device.imei == self.device_imei).first()
                
                if device:
                    location = Location(
                        device_id=device.id,
                        latitude=data['latitude'],
                        longitude=data['longitude'],
                        speed=data['speed'],
                        course=data['course'],
                        satellites=data['satellites'],
                        gps_valid=data['gps_valid'],
                        timestamp=data['timestamp'],
                        is_alarm=True,
                        alarm_type=alarm_type
                    )
                    db.add(location)
                    
                    device.last_latitude = data['latitude']
                    device.last_longitude = data['longitude']
                    device.last_update = datetime.utcnow()
                    
                    db.commit()
                    
                    # Broadcast alarm to WebSocket clients
                    await self.server.broadcast_alarm(device.id, data)
            
            finally:
                db.close()
        
        except Exception as e:
            logger.error(f"Error handling alarm: {e}", exc_info=True)
    
    async def handle_command_response(self, data: Dict):
        """Handle 0x15 command response from terminal"""
        content = data.get('content', '')
        logger.info(f"Command response from {self.device_imei}: {content}")
        if self._pending_response and not self._pending_response.done():
            self._pending_response.set_result(data)
    
    async def send_command(self, command: str, timeout: float = 10.0) -> Dict:
        """Send an ASCII command to the device (Protocol 0x80) and wait for the 0x15 response."""
        self._command_serial = (self._command_serial + 1) & 0xFFFF
        packet = self.parser.create_command_packet(command, self._command_serial)
        if not packet:
            return {"success": False, "error": "Failed to build command packet"}

        loop = asyncio.get_event_loop()
        self._pending_response = loop.create_future()

        await self.send_data(packet)
        logger.info(f"Sent command to {self.device_imei}: {command}")

        try:
            result = await asyncio.wait_for(self._pending_response, timeout=timeout)
            return {
                "success": True,
                "response": result.get('content', ''),
                "server_flag": result.get('server_flag'),
            }
        except asyncio.TimeoutError:
            logger.warning(f"Command timed out for {self.device_imei}: {command}")
            return {"success": True, "response": None, "note": "Command sent but device did not reply within timeout. It may still take effect."}
        finally:
            self._pending_response = None
    
    async def send_data(self, data: bytes):
        """Send data to GPS tracker"""
        try:
            self.writer.write(data)
            await self.writer.drain()
            logger.debug(f"Sent response ({len(data)} bytes): {data.hex()}")
        except Exception as e:
            logger.error(f"Error sending data: {e}")
    
    async def close(self):
        """Close connection"""
        try:
            if self.device_imei:
                self.server.unregister_device(self.device_imei)
                
                # Update device status
                db = SessionLocal()
                try:
                    device = db.query(Device).filter(Device.imei == self.device_imei).first()
                    if device:
                        device.status = 'offline'
                        db.commit()
                finally:
                    db.close()
            
            self.writer.close()
            await self.writer.wait_closed()
            logger.info(f"Connection closed for {self.address}")
        
        except Exception as e:
            logger.error(f"Error closing connection: {e}")


class TCPServer:
    """TCP server for GPS trackers"""
    
    def __init__(self, host: str = '0.0.0.0', port: int = 7018):
        self.host = host
        self.port = port
        self.server = None
        self.connections: Set[GPSTrackerConnection] = set()
        self.device_connections: Dict[str, GPSTrackerConnection] = {}
        self.websocket_clients: Set = set()
    
    async def start(self):
        """Start TCP server"""
        self.server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port
        )
        
        addr = self.server.sockets[0].getsockname()
        logger.info(f"GPS Tracker TCP Server started on {addr[0]}:{addr[1]}")
        
        async with self.server:
            await self.server.serve_forever()
    
    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle new client connection"""
        address = writer.get_extra_info('peername')
        connection = GPSTrackerConnection(reader, writer, address, self)
        self.connections.add(connection)
        
        try:
            await connection.handle()
        finally:
            self.connections.discard(connection)
    
    def register_device(self, imei: str, connection: GPSTrackerConnection):
        """Register device connection"""
        self.device_connections[imei] = connection
        logger.info(f"Registered device: {imei} (total: {len(self.device_connections)})")
    
    def unregister_device(self, imei: str):
        """Unregister device connection"""
        if imei in self.device_connections:
            del self.device_connections[imei]
            logger.info(f"Unregistered device: {imei} (total: {len(self.device_connections)})")
    
    async def broadcast_location_update(self, device_id: int, data: Dict):
        """Broadcast location update to WebSocket clients"""
        # Will be implemented with WebSocket support
        pass
    
    async def broadcast_alarm(self, device_id: int, data: Dict):
        """Broadcast alarm to WebSocket clients"""
        # Will be implemented with WebSocket support
        pass
    
    async def send_command_to_device(self, imei: str, command: str, timeout: float = 10.0) -> Dict:
        """Send an ASCII command to a connected device and return the response."""
        connection = self.device_connections.get(imei)
        if not connection:
            return {"success": False, "error": "Device not connected"}
        return await connection.send_command(command, timeout=timeout)

# API Endpoints Documentation
## BYThron GPS Tracking Server

**Base URL:** `http://your-server-address:port`  
**API Version:** As defined in settings  
**Last Updated:** February 8, 2026

---

## Table of Contents

1. [Core Endpoints](#core-endpoints)
2. [Authentication Endpoints](#authentication-endpoints)
3. [Device Management Endpoints](#device-management-endpoints)
4. [Location Tracking Endpoints](#location-tracking-endpoints)
5. [Dashboard Endpoints](#dashboard-endpoints)

---

## Core Endpoints

### 1. Root Endpoint
**`GET /`**

Returns basic server information and status.

**Response:**
```json
{
  "app": "GPS Tracking Server",
  "version": "1.0.0",
  "status": "running",
  "tcp_port": 8000,
  "http_port": 8080
}
```

**Role:** System health check and information endpoint for clients to verify server connectivity and version.

---

### 2. Health Check
**`GET /health`**

Returns server health status and connection statistics.

**Response:**
```json
{
  "status": "healthy",
  "tcp_connections": 5,
  "active_devices": 3
}
```

**Role:** Monitoring endpoint for load balancers and health check systems to verify server operation and get active connection counts.

---

## Authentication Endpoints

All authentication endpoints are under `/api/auth` prefix.

### 1. Sync User
**`POST /api/auth/sync`**

Creates or updates a user record based on Clerk authentication data.

**Headers:** None required

**Request Body:**
```json
{
  "clerk_user_id": "user_2abc123xyz456def",
  "email": "user@example.com",
  "name": "John Doe"
}
```

**Response (200):**
```json
{
  "id": 1,
  "clerk_user_id": "user_2abc123xyz456def",
  "email": "user@example.com",
  "name": "John Doe",
  "created_at": "2026-02-08T10:00:00Z",
  "updated_at": "2026-02-08T10:00:00Z"
}
```

**Role:** Upsert operation that synchronizes Clerk-authenticated users to the local database. Called automatically when users sign in to the mobile app.

---

### 2. Get User
**`GET /api/auth/user/{clerk_user_id}`**

Retrieves user information by Clerk user ID.

**Path Parameters:**
- `clerk_user_id` (string): Clerk user identifier

**Response (200):**
```json
{
  "id": 1,
  "clerk_user_id": "user_2abc123xyz456def",
  "email": "user@example.com",
  "name": "John Doe",
  "created_at": "2026-02-08T10:00:00Z",
  "updated_at": "2026-02-08T10:00:00Z"
}
```

**Error Responses:**
- `404`: User not found

**Role:** Retrieve user profile information for display in mobile apps or admin interfaces.

---

## Device Management Endpoints

All device endpoints are under `/api/devices` prefix.

### 1. List Devices
**`GET /api/devices/`**

Lists all GPS tracker devices with optional filtering.

**Headers (Optional):**
- `X-Clerk-User-Id`: Filter devices by user ownership

**Query Parameters:**
- `skip` (int, default: 0): Number of records to skip for pagination
- `limit` (int, default: 100, max: 1000): Maximum number of records to return
- `status` (string): Filter by status (`online`, `offline`)

**Response (200):**
```json
[
  {
    "id": 1,
    "imei": "123456789012345",
    "name": "Company Vehicle 1",
    "description": "Main delivery truck",
    "status": "online",
    "user_id": 1,
    "last_connect": "2026-02-08T10:30:00Z",
    "last_update": "2026-02-08T10:35:00Z",
    "last_latitude": 40.7128,
    "last_longitude": -74.0060,
    "battery_level": 85,
    "gsm_signal": 25,
    "created_at": "2026-01-01T00:00:00Z"
  }
]
```

**Role:** Main device listing endpoint. Returns all devices or filters by user when authenticated. Used by mobile apps to show user's device list and by admin interfaces for fleet management.

---

### 2. Get Device by ID
**`GET /api/devices/{device_id}`**

Retrieves detailed information about a specific device.

**Path Parameters:**
- `device_id` (int): Device ID

**Response (200):** Same as single device object above

**Error Responses:**
- `404`: Device not found

**Role:** Fetch detailed information about a specific device for device detail pages.

---

### 3. Get Device by IMEI
**`GET /api/devices/imei/{imei}`**

Retrieves device information using its IMEI number.

**Path Parameters:**
- `imei` (string): Device IMEI number

**Response (200):** Same as device object

**Error Responses:**
- `404`: Device not found

**Role:** Alternative device lookup method using IMEI, useful for device registration workflows or when device ID is not yet known.

---

### 4. Create Device
**`POST /api/devices/`**

Manually registers a new GPS tracker device.

**Headers (Optional):**
- `X-Clerk-User-Id`: Associates device with user

**Request Body:**
```json
{
  "imei": "123456789012345",
  "name": "Company Vehicle 1",
  "description": "Main delivery truck"
}
```

**Response (201):** Returns created device object

**Error Responses:**
- `400`: Device with this IMEI already exists

**Role:** Manual device registration. Allows users or admins to pre-register devices before first connection. Can automatically associate with user if authenticated.

---

### 5. Update Device
**`PUT /api/devices/{device_id}`**

Updates device information (name and description).

**Path Parameters:**
- `device_id` (int): Device ID

**Request Body:**
```json
{
  "name": "Updated Vehicle Name",
  "description": "Updated description"
}
```

**Response (200):** Returns updated device object

**Error Responses:**
- `404`: Device not found

**Role:** Edit device metadata like friendly names and descriptions. Used in device settings pages.

---

### 6. Delete Device
**`DELETE /api/devices/{device_id}`**

Removes a device from the system.

**Path Parameters:**
- `device_id` (int): Device ID

**Response (204):** No content

**Error Responses:**
- `404`: Device not found

**Role:** Permanent device removal. Use with caution as this may cascade delete location history depending on database constraints.

---

### 7. Assign Device to User
**`POST /api/devices/{device_id}/assign`**

Associates a device with a specific user.

**Path Parameters:**
- `device_id` (int): Device ID

**Headers (Required):**
- `X-Clerk-User-Id`: User to assign device to

**Response (200):**
```json
{
  "message": "Device assigned successfully",
  "device_id": 1,
  "user_id": 1,
  "clerk_user_id": "user_2abc123xyz456def"
}
```

**Error Responses:**
- `404`: Device or user not found

**Role:** Device ownership management. Allows assigning unassigned devices to users or transferring device ownership.

---

### 8. Get Device Status
**`GET /api/devices/{device_id}/status`**

Retrieves current device status summary.

**Path Parameters:**
- `device_id` (int): Device ID

**Response (200):**
```json
{
  "id": 1,
  "imei": "123456789012345",
  "name": "Company Vehicle 1",
  "status": "online",
  "last_update": "2026-02-08T10:35:00Z",
  "battery_level": 85,
  "gsm_signal": 25,
  "location": {
    "latitude": 40.7128,
    "longitude": -74.0060
  }
}
```

**Error Responses:**
- `404`: Device not found

**Role:** Quick status check for devices. Returns essential operational data like battery, signal, and last known position. Useful for dashboard widgets.

---

## Location Tracking Endpoints

All location endpoints are under `/api/locations` prefix.

### 1. Get Latest Location
**`GET /api/locations/{device_id}/latest`**

Retrieves the most recent location data for a device.

**Path Parameters:**
- `device_id` (int): Device ID

**Headers (Optional):**
- `X-Clerk-User-Id`: Verifies user has access to device

**Response (200):**
```json
{
  "id": 12345,
  "device_id": 1,
  "latitude": 40.7128,
  "longitude": -74.0060,
  "speed": 45.5,
  "course": 180,
  "satellites": 12,
  "gps_valid": true,
  "is_alarm": false,
  "alarm_type": null,
  "timestamp": "2026-02-08T10:35:00Z",
  "received_at": "2026-02-08T10:35:02Z"
}
```

**Error Responses:**
- `403`: Access denied to this device
- `404`: Device or location data not found

**Role:** Primary endpoint for real-time device location. Used by mobile apps and dashboards to show current device position on maps.

---

### 2. Get Location History
**`GET /api/locations/{device_id}/history`**

Retrieves historical location data for route playback and analysis.

**Path Parameters:**
- `device_id` (int): Device ID

**Headers (Optional):**
- `X-Clerk-User-Id`: Verifies user access

**Query Parameters:**
- `start_time` (datetime, optional): Start time in UTC (default: 24 hours ago)
- `end_time` (datetime, optional): End time in UTC (default: now)
- `limit` (int, default: 1000, max: 10000): Maximum points to return

**Response (200):**
```json
{
  "device_id": 1,
  "device_name": "Company Vehicle 1",
  "device_imei": "123456789012345",
  "total_points": 1500,
  "locations": [
    {
      "id": 12345,
      "device_id": 1,
      "latitude": 40.7128,
      "longitude": -74.0060,
      "speed": 45.5,
      "course": 180,
      "satellites": 12,
      "gps_valid": true,
      "is_alarm": false,
      "alarm_type": null,
      "timestamp": "2026-02-08T10:35:00Z",
      "received_at": "2026-02-08T10:35:02Z"
    }
  ]
}
```

**Error Responses:**
- `403`: Access denied
- `404`: Device not found

**Role:** Historical tracking data for route visualization, trip analysis, and reporting. Essential for fleet management and activity logs.

---

### 3. Get Device Route
**`GET /api/locations/{device_id}/route`**

Returns route data in GeoJSON format optimized for map display.

**Path Parameters:**
- `device_id` (int): Device ID

**Headers (Optional):**
- `X-Clerk-User-Id`: Verifies user access

**Query Parameters:**
- `start_time` (datetime, optional): Start time in UTC (default: 24 hours ago)
- `end_time` (datetime, optional): End time in UTC (default: now)
- `simplify` (bool, default: false): Simplify route to reduce points

**Response (200):**
```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": {
        "type": "Point",
        "coordinates": [-74.0060, 40.7128]
      },
      "properties": {
        "timestamp": "2026-02-08T10:35:00Z",
        "speed": 45.5,
        "course": 180,
        "is_alarm": false
      }
    }
  ],
  "properties": {
    "device_id": 1,
    "device_name": "Company Vehicle 1",
    "start_time": "2026-02-07T10:35:00Z",
    "end_time": "2026-02-08T10:35:00Z",
    "point_count": 1500
  }
}
```

**Error Responses:**
- `403`: Access denied
- `404`: Device not found

**Role:** GeoJSON formatted route data for direct consumption by mapping libraries (Leaflet, Mapbox, Google Maps). Optimized for visualization.

---

### 4. Get Device Alarms
**`GET /api/locations/{device_id}/alarms`**

Retrieves alarm events (SOS, geofence violations, etc.) for a device.

**Path Parameters:**
- `device_id` (int): Device ID

**Query Parameters:**
- `start_time` (datetime, optional): Start time in UTC (default: 7 days ago)
- `end_time` (datetime, optional): End time in UTC (default: now)
- `limit` (int, default: 100, max: 1000): Maximum alarms to return

**Response (200):** Array of location objects where `is_alarm: true`

**Role:** Security and alert monitoring. Shows SOS button presses, geofence breaches, and other alarm events. Critical for emergency response.

---

### 5. Get Nearby Devices
**`GET /api/locations/nearby`**

Finds devices within a specified radius of a location.

**Query Parameters:**
- `latitude` (float, required): Center latitude (-90 to 90)
- `longitude` (float, required): Center longitude (-180 to 180)
- `radius_km` (float, default: 10, max: 100): Search radius in kilometers

**Response (200):**
```json
{
  "center": {
    "latitude": 40.7128,
    "longitude": -74.0060
  },
  "radius_km": 10,
  "devices_found": 3,
  "devices": [
    {
      "device_id": 1,
      "device_name": "Company Vehicle 1",
      "imei": "123456789012345",
      "latitude": 40.7130,
      "longitude": -74.0062,
      "distance_km": 0.25,
      "last_update": "2026-02-08T10:35:00Z"
    }
  ]
}
```

**Role:** Proximity search for fleet coordination. Find nearby vehicles for dispatch optimization or collaborative operations.

---

## Dashboard Endpoints

### 1. Web Dashboard
**`GET /dashboard`**

Renders a web-based dashboard showing all devices with their status, location, and activity.

**Response:** HTML page

**Features Displayed:**
- Device list with names and IMEI numbers
- Connection status (online/offline)
- Battery levels with icons 🔋🪫
- GSM signal strength with bars 📶
- Last known location coordinates
- Last update timestamp
- Movement status (moving, stationary, never moved)
- Current speed and satellite count
- Total device count
- Online device count

**Role:** Human-readable web interface for monitoring fleet status. Accessible from any web browser without authentication (suitable for internal networks or add authentication as needed).

---

## Authentication & Authorization

### Clerk Integration

The API uses Clerk for user authentication with the following pattern:

1. **User Sync**: Mobile app calls `/api/auth/sync` after user signs in with Clerk
2. **Header-Based Auth**: API endpoints accept optional `X-Clerk-User-Id` header
3. **Access Control**: When header is provided, endpoints filter data by user ownership
4. **Backward Compatibility**: Endpoints work without auth headers (returns all data)

### Protected Resources

Devices and locations can be filtered by user:
- Devices: List and create operations respect `X-Clerk-User-Id`
- Locations: Read operations verify device ownership when authenticated
- Assignment: Devices can be explicitly assigned to users

---

## Error Responses

All endpoints follow standard HTTP status codes:

- **200 OK**: Successful GET/PUT/POST
- **201 Created**: Successful resource creation
- **204 No Content**: Successful DELETE
- **400 Bad Request**: Invalid request data
- **403 Forbidden**: Access denied to resource
- **404 Not Found**: Resource doesn't exist
- **500 Internal Server Error**: Server-side error

Error response format:
```json
{
  "detail": "Error message describing what went wrong"
}
```

---

## Rate Limiting

Not currently implemented. Consider adding rate limiting for production deployments.

---

## CORS Configuration

CORS is enabled for all origins by default. Configure `CORS_ORIGINS` in settings for production.

---

## TCP Connection

In addition to HTTP APIs, the server runs a TCP server on port 8000 (configurable) that:
- Accepts connections from GPS tracker devices
- Parses proprietary GPS protocols
- Automatically creates/updates device records
- Stores location data in real-time
- Updates device status and last-seen timestamps

The TCP server runs in the background alongside the HTTP API server.

---

## Notes for Developers

1. **Database Sessions**: All endpoints use SQLAlchemy ORM with automatic session management via `Depends(get_db)`

2. **Timestamps**: All datetime values are in UTC and use ISO 8601 format

3. **Pagination**: List endpoints support `skip` and `limit` for pagination

4. **Optional Filtering**: Many endpoints accept optional query parameters for filtering

5. **User Context**: Most endpoints can operate with or without user context for flexibility

6. **Auto-Registration**: Devices auto-register when they first connect via TCP

7. **Real-time Updates**: Device status, battery, and location are updated automatically by TCP server

8. **GeoJSON Support**: Route endpoints return standard GeoJSON for easy map integration

---

## Architecture Overview

```
Mobile App / Web Client
        ↓
   HTTP API Server (FastAPI)
   ├── Authentication (/api/auth)
   ├── Devices (/api/devices)
   ├── Locations (/api/locations)
   └── Dashboard (/dashboard)
        ↓
   PostgreSQL Database
        ↑
   TCP Server (Background)
        ↑
  GPS Tracker Devices
```

---

## Future Enhancements

Potential additions not yet implemented:
- WebSocket support for real-time location streaming
- Geofence management endpoints
- User management (CRUD operations beyond sync)
- Notification/alert configuration
- Trip/route analysis endpoints
- Bulk device operations
- Export capabilities (CSV, GPX)
- Advanced analytics endpoints

---

*This documentation reflects the current state of the API as of February 8, 2026.*

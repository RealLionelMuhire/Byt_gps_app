# Clerk Authentication Integration - Implementation Guide

## ✅ Implementation Complete

The backend has been successfully updated to support Clerk authentication and user management. Here's what was implemented:

---

## 📋 Changes Made

### 1. **User Model** ([server/app/models/user.py](server/app/models/user.py))
- Redesigned to support Clerk authentication
- Added fields: `clerk_user_id`, `email`, `name`
- Removed old authentication fields (hashed_password, etc.)
- Added timestamps: `created_at`, `updated_at`

### 2. **Database Schema** ([server/init_db.sql](server/init_db.sql))
- Added users table with proper indexes
- Created trigger for auto-updating `updated_at` timestamp
- Added indexes on `clerk_user_id` and `email` for fast lookups

### 3. **Authentication Endpoint** ([server/app/api/auth.py](server/app/api/auth.py))
- **POST `/api/auth/sync`** - Main user sync endpoint
  - Creates or updates users from Clerk data
  - Validates required fields
  - Returns user data with timestamps
- **GET `/api/auth/user/{clerk_user_id}`** - Get user by Clerk ID

### 4. **Device Model** ([server/app/models/device.py](server/app/models/device.py))
- Added `user_id` foreign key to link devices to users
- Added relationship to User model

### 5. **Devices API** ([server/app/api/devices.py](server/app/api/devices.py))
- **User Filtering**: All device endpoints now accept `X-Clerk-User-Id` header
- **GET `/api/devices`** - Lists only user's devices when header provided
- **POST `/api/devices`** - Auto-assigns device to user when creating
- **POST `/api/devices/{device_id}/assign`** - Assign existing device to user

### 6. **Locations API** ([server/app/api/locations.py](server/app/api/locations.py))
- Added access control to all location endpoints
- **GET `/api/locations/{device_id}/latest`** - Verifies device ownership
- **GET `/api/locations/{device_id}/history`** - Verifies device ownership
- **GET `/api/locations/{device_id}/route`** - Verifies device ownership

### 7. **Main Application** ([server/app/main.py](server/app/main.py))
- Registered auth router at `/api/auth`
- Enabled CORS for mobile app access

---

## 🚀 API Endpoints

### Authentication

#### **POST /api/auth/sync**
Sync Clerk user to database (upsert operation)

**Request:**
```bash
curl -X POST http://164.92.212.186:8000/api/auth/sync \
  -H "Content-Type: application/json" \
  -d '{
    "clerk_user_id": "user_2abc123xyz456def",
    "email": "user@example.com",
    "name": "John Doe"
  }'
```

**Response (200 OK):**
```json
{
  "id": 1,
  "clerk_user_id": "user_2abc123xyz456def",
  "email": "user@example.com",
  "name": "John Doe",
  "created_at": "2026-02-06T10:00:00Z",
  "updated_at": "2026-02-06T10:00:00Z"
}
```

#### **GET /api/auth/user/{clerk_user_id}**
Get user by Clerk ID

---

### Devices (with User Filtering)

All device endpoints now accept the `X-Clerk-User-Id` header for user filtering.

#### **GET /api/devices**
List devices (filtered by user if header provided)

**Request:**
```bash
curl -X GET http://164.92.212.186:8000/api/devices \
  -H "X-Clerk-User-Id: user_2abc123xyz456def"
```

#### **POST /api/devices**
Create device (auto-assigned to user)

**Request:**
```bash
curl -X POST http://164.92.212.186:8000/api/devices \
  -H "Content-Type: application/json" \
  -H "X-Clerk-User-Id: user_2abc123xyz456def" \
  -d '{
    "imei": "123456789012345",
    "name": "My GPS Tracker",
    "description": "Vehicle tracker"
  }'
```

#### **POST /api/devices/{device_id}/assign**
Assign existing device to user

**Request:**
```bash
curl -X POST http://164.92.212.186:8000/api/devices/1/assign \
  -H "X-Clerk-User-Id: user_2abc123xyz456def"
```

---

### Locations (with Access Control)

All location endpoints verify device ownership when `X-Clerk-User-Id` header is provided.

#### **GET /api/locations/{device_id}/latest**
Get latest location for device

**Request:**
```bash
curl -X GET http://164.92.212.186:8000/api/locations/1/latest \
  -H "X-Clerk-User-Id: user_2abc123xyz456def"
```

#### **GET /api/locations/{device_id}/history**
Get location history

**Request:**
```bash
curl -X GET "http://164.92.212.186:8000/api/locations/1/history?limit=100" \
  -H "X-Clerk-User-Id: user_2abc123xyz456def"
```

#### **GET /api/locations/{device_id}/route**
Get device route (for map display)

**Request:**
```bash
curl -X GET "http://164.92.212.186:8000/api/locations/1/route" \
  -H "X-Clerk-User-Id: user_2abc123xyz456def"
```

---

## 🔐 Security Model

### Header-Based Authentication
- All endpoints accept **`X-Clerk-User-Id`** header
- When provided, the backend:
  1. Looks up the user by `clerk_user_id`
  2. Filters data to only that user's devices
  3. Verifies access permissions

### Backward Compatibility
- All endpoints work **without** the header (returns all data)
- Allows gradual migration from old to new authentication

### Device Ownership
- Devices can be assigned to users via:
  1. Setting header when creating device
  2. Using `/api/devices/{device_id}/assign` endpoint
  3. Direct database update (for migration)

---

## 📊 Database Schema

### Users Table
```sql
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    clerk_user_id VARCHAR(255) UNIQUE NOT NULL,
    email VARCHAR(255) NOT NULL,
    name VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_clerk_user_id ON users(clerk_user_id);
CREATE INDEX idx_email ON users(email);
```

### Devices Table (Updated)
```sql
ALTER TABLE devices 
  ADD COLUMN user_id INTEGER REFERENCES users(id);

CREATE INDEX idx_devices_user_id ON devices(user_id);
```

---

## 🧪 Testing

### Test Script
A test script is provided: [server/test_clerk_auth.py](server/test_clerk_auth.py)

```bash
cd server
python test_clerk_auth.py
```

### Manual Testing

1. **Sync a user:**
```bash
curl -X POST http://164.92.212.186:8000/api/auth/sync \
  -H "Content-Type: application/json" \
  -d '{
    "clerk_user_id": "user_test123",
    "email": "test@example.com",
    "name": "Test User"
  }'
```

2. **Create a device for the user:**
```bash
curl -X POST http://164.92.212.186:8000/api/devices \
  -H "Content-Type: application/json" \
  -H "X-Clerk-User-Id: user_test123" \
  -d '{
    "imei": "999888777666555",
    "name": "Test Tracker",
    "description": "Testing device"
  }'
```

3. **List user's devices:**
```bash
curl -X GET http://164.92.212.186:8000/api/devices \
  -H "X-Clerk-User-Id: user_test123"
```

---

## 🔄 Migration Steps

### For Existing Database

1. **Backup your database:**
```bash
pg_dump gps_tracking > backup.sql
```

2. **Run the updated init_db.sql:**
```bash
psql -U gps_user -d gps_tracking -f init_db.sql
```

3. **Add user_id column to devices:**
```sql
ALTER TABLE devices ADD COLUMN IF NOT EXISTS user_id INTEGER;
ALTER TABLE devices ADD CONSTRAINT fk_devices_user 
  FOREIGN KEY (user_id) REFERENCES users(id);
CREATE INDEX IF NOT EXISTS idx_devices_user_id ON devices(user_id);
```

4. **Restart the server:**
```bash
cd server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---

## 📱 Mobile App Integration

### Sign In Flow

```javascript
// 1. User signs in with Clerk
const { user } = await clerk.signIn({...});

// 2. Sync user to backend
const response = await fetch('http://164.92.212.186:8000/api/auth/sync', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
  },
  body: JSON.stringify({
    clerk_user_id: user.id,
    email: user.emailAddresses[0].emailAddress,
    name: user.fullName,
  }),
});

const userData = await response.json();
console.log('User synced:', userData);

// 3. Store clerk_user_id for future requests
await AsyncStorage.setItem('clerk_user_id', user.id);
```

### Fetching User's Devices

```javascript
const clerkUserId = await AsyncStorage.getItem('clerk_user_id');

const response = await fetch('http://164.92.212.186:8000/api/devices', {
  headers: {
    'X-Clerk-User-Id': clerkUserId,
  },
});

const devices = await response.json();
console.log('My devices:', devices);
```

### Fetching Device Location

```javascript
const clerkUserId = await AsyncStorage.getItem('clerk_user_id');
const deviceId = 1;

const response = await fetch(
  `http://164.92.212.186:8000/api/locations/${deviceId}/latest`,
  {
    headers: {
      'X-Clerk-User-Id': clerkUserId,
    },
  }
);

const location = await response.json();
console.log('Latest location:', location);
```

---

## 🎯 Key Features

✅ **Upsert Operation** - Creates or updates users automatically  
✅ **Idempotent** - Safe to call multiple times with same data  
✅ **User Filtering** - Automatically filters data by user  
✅ **Access Control** - Verifies device ownership  
✅ **Backward Compatible** - Works without authentication header  
✅ **Auto Timestamps** - Tracks creation and update times  
✅ **Indexed Lookups** - Fast queries on clerk_user_id  
✅ **Device Assignment** - Easy device ownership management  

---

## 📚 API Documentation

Full interactive API documentation available at:
- **Swagger UI**: http://164.92.212.186:8000/docs
- **ReDoc**: http://164.92.212.186:8000/redoc

---

## 🐛 Troubleshooting

### Issue: "User not found"
**Solution:** User must be synced first via `/api/auth/sync` before accessing other endpoints.

### Issue: "Access denied to this device"
**Solution:** Device is not assigned to the user. Use `/api/devices/{device_id}/assign` to assign it.

### Issue: "Device not found"
**Solution:** Check device ID is correct and user has permission to access it.

### Issue: Database errors after migration
**Solution:** 
1. Check that users table exists: `\dt users` in psql
2. Run init_db.sql again
3. Add user_id column to devices table manually

---

## ✨ Next Steps

1. **Deploy Updated Code**: Push to server and restart
2. **Run Database Migrations**: Execute the SQL migration scripts
3. **Test Endpoints**: Use provided test script or cURL commands
4. **Update Mobile App**: Implement user sync on sign-in
5. **Add Header to Requests**: Include `X-Clerk-User-Id` in all API calls

---

## 📞 Support

For issues or questions:
- Check server logs: `tail -f server/logs/app.log`
- Review API docs: http://164.92.212.186:8000/docs
- Test with cURL before implementing in mobile app

---

**Implementation Date**: February 6, 2026  
**Backend Version**: 1.0.0  
**Database**: PostgreSQL with PostGIS

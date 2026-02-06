# 🚀 Quick Setup Guide - Clerk Authentication

## Prerequisites
- Python 3.8+
- PostgreSQL with PostGIS
- Existing GPS tracking backend

---

## Setup Steps

### 1. **Update Code Files** ✅
All code files have been updated. Changes include:
- `server/app/models/user.py` - User model
- `server/app/models/device.py` - Device model with user relationship
- `server/app/api/auth.py` - NEW authentication endpoints
- `server/app/api/devices.py` - User filtering added
- `server/app/api/locations.py` - Access control added
- `server/app/main.py` - Auth router registered
- `server/init_db.sql` - Users table schema

### 2. **Run Database Migration**

Option A: Fresh Installation
```bash
cd server
psql -U gps_user -d gps_tracking -f init_db.sql
```

Option B: Existing Database (Recommended)
```bash
cd server
psql -U gps_user -d gps_tracking -f migrations/001_add_clerk_auth.sql
```

### 3. **Verify Database Schema**
```bash
psql -U gps_user -d gps_tracking -c "\d users"
psql -U gps_user -d gps_tracking -c "\d devices"
```

You should see:
- `users` table with columns: id, clerk_user_id, email, name, created_at, updated_at
- `devices` table with new column: user_id

### 4. **Restart Server**
```bash
cd server
# Kill existing process
pkill -f "uvicorn app.main:app"

# Start server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Or with systemd:
```bash
sudo systemctl restart gps-tracking
```

### 5. **Test the Implementation**
```bash
cd server
python test_clerk_auth.py
```

Expected output:
```
✅ PASS - Create User via Sync
✅ PASS - Update User via Sync
✅ PASS - Get User by Clerk ID
✅ PASS - Create Device with User
✅ PASS - List User's Devices
...
```

### 6. **Manual API Test**

Test user sync:
```bash
curl -X POST http://localhost:8000/api/auth/sync \
  -H "Content-Type: application/json" \
  -d '{
    "clerk_user_id": "user_test123",
    "email": "test@example.com",
    "name": "Test User"
  }'
```

Expected response:
```json
{
  "id": 1,
  "clerk_user_id": "user_test123",
  "email": "test@example.com",
  "name": "Test User",
  "created_at": "2026-02-06T...",
  "updated_at": "2026-02-06T..."
}
```

### 7. **Check API Documentation**
Open in browser:
- http://localhost:8000/docs (Swagger UI)
- http://localhost:8000/redoc (ReDoc)

You should see new endpoints:
- `POST /api/auth/sync`
- `GET /api/auth/user/{clerk_user_id}`

---

## Configuration

### Environment Variables (Optional)
```bash
# .env file
DATABASE_URL=postgresql://gps_user:gps_password@localhost:5432/gps_tracking
SECRET_KEY=your-secret-key-here
CORS_ORIGINS=["*"]
```

### CORS Settings
Already configured in [server/app/main.py](server/app/main.py):
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,  # ["*"] by default
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

---

## Mobile App Integration

### 1. User Sign-In Flow
```javascript
// After Clerk authentication
const syncUser = async (clerkUser) => {
  const response = await fetch('http://164.92.212.186:8000/api/auth/sync', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      clerk_user_id: clerkUser.id,
      email: clerkUser.emailAddresses[0].emailAddress,
      name: clerkUser.fullName,
    }),
  });
  
  const userData = await response.json();
  
  // Store for future use
  await AsyncStorage.setItem('clerk_user_id', clerkUser.id);
  await AsyncStorage.setItem('user_data', JSON.stringify(userData));
  
  return userData;
};
```

### 2. Add Header to All Requests
```javascript
const makeAuthRequest = async (endpoint, options = {}) => {
  const clerkUserId = await AsyncStorage.getItem('clerk_user_id');
  
  return fetch(`http://164.92.212.186:8000${endpoint}`, {
    ...options,
    headers: {
      ...options.headers,
      'X-Clerk-User-Id': clerkUserId,
    },
  });
};

// Usage
const devices = await makeAuthRequest('/api/devices');
const location = await makeAuthRequest('/api/locations/1/latest');
```

---

## Troubleshooting

### Issue: Migration fails with "relation already exists"
**Solution:** This is normal if running init_db.sql on existing database. Use migration script instead:
```bash
psql -U gps_user -d gps_tracking -f migrations/001_add_clerk_auth.sql
```

### Issue: Server won't start - Import errors
**Solution:** Ensure all dependencies are installed:
```bash
cd server
pip install -r requirements.txt
```

### Issue: "User not found" errors
**Solution:** Users must be synced via `/api/auth/sync` before using other endpoints.

### Issue: Devices not filtered by user
**Solution:** Ensure you're sending the `X-Clerk-User-Id` header in requests.

### Issue: Can't connect to database
**Solution:** Check database credentials in environment or config:
```bash
psql -U gps_user -d gps_tracking -c "SELECT 1;"
```

---

## Verification Checklist

- [ ] Database migration completed successfully
- [ ] Server restarts without errors
- [ ] `/api/docs` shows auth endpoints
- [ ] Test script passes all tests
- [ ] Can sync user via cURL
- [ ] Can create device with user header
- [ ] Can filter devices by user
- [ ] Mobile app can authenticate

---

## Next Steps

1. **Deploy to Production**
   ```bash
   # On production server
   cd /path/to/Byt_gps_app/server
   git pull
   psql -U gps_user -d gps_tracking -f migrations/001_add_clerk_auth.sql
   sudo systemctl restart gps-tracking
   ```

2. **Update Mobile App**
   - Implement user sync on sign-in
   - Add `X-Clerk-User-Id` header to all API calls
   - Test with production server

3. **Monitor Logs**
   ```bash
   tail -f /var/log/gps-tracking.log
   # or
   journalctl -u gps-tracking -f
   ```

---

## Support & Documentation

- **Full Documentation**: [server/CLERK_AUTH_IMPLEMENTATION.md](CLERK_AUTH_IMPLEMENTATION.md)
- **API Docs**: http://164.92.212.186:8000/docs
- **Test Script**: [server/test_clerk_auth.py](test_clerk_auth.py)
- **Migration Script**: [server/migrations/001_add_clerk_auth.sql](migrations/001_add_clerk_auth.sql)

---

**Setup Time**: ~5-10 minutes  
**Status**: ✅ Ready to deploy

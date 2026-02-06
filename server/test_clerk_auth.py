#!/usr/bin/env python3
"""
Test script for Clerk authentication endpoints
Tests the user sync and device filtering functionality
"""

import requests
import json
from datetime import datetime

# Configuration
BASE_URL = "http://localhost:8000"  # Change to http://164.92.212.186:8000 for production
TEST_CLERK_USER_ID = "user_test_" + datetime.now().strftime("%Y%m%d%H%M%S")
TEST_EMAIL = f"test_{datetime.now().strftime('%Y%m%d%H%M%S')}@example.com"
TEST_NAME = "Test User"
TEST_IMEI = "TEST" + datetime.now().strftime("%Y%m%d%H%M%S")

def print_section(title):
    """Print a section header"""
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)

def print_result(test_name, success, data=None):
    """Print test result"""
    status = "✅ PASS" if success else "❌ FAIL"
    print(f"\n{status} - {test_name}")
    if data:
        print(f"Response: {json.dumps(data, indent=2, default=str)}")

def test_user_sync():
    """Test POST /api/auth/sync endpoint"""
    print_section("TEST 1: User Sync (Create)")
    
    url = f"{BASE_URL}/api/auth/sync"
    payload = {
        "clerk_user_id": TEST_CLERK_USER_ID,
        "email": TEST_EMAIL,
        "name": TEST_NAME
    }
    
    try:
        response = requests.post(url, json=payload)
        success = response.status_code == 200
        data = response.json() if success else {"error": response.text}
        print_result("Create User via Sync", success, data)
        return data if success else None
    except Exception as e:
        print_result("Create User via Sync", False, {"error": str(e)})
        return None

def test_user_sync_update():
    """Test POST /api/auth/sync endpoint (update)"""
    print_section("TEST 2: User Sync (Update)")
    
    url = f"{BASE_URL}/api/auth/sync"
    payload = {
        "clerk_user_id": TEST_CLERK_USER_ID,
        "email": TEST_EMAIL,
        "name": "Updated Test User"
    }
    
    try:
        response = requests.post(url, json=payload)
        success = response.status_code == 200
        data = response.json() if success else {"error": response.text}
        print_result("Update User via Sync", success, data)
        return data if success else None
    except Exception as e:
        print_result("Update User via Sync", False, {"error": str(e)})
        return None

def test_get_user():
    """Test GET /api/auth/user/{clerk_user_id} endpoint"""
    print_section("TEST 3: Get User by Clerk ID")
    
    url = f"{BASE_URL}/api/auth/user/{TEST_CLERK_USER_ID}"
    
    try:
        response = requests.get(url)
        success = response.status_code == 200
        data = response.json() if success else {"error": response.text}
        print_result("Get User by Clerk ID", success, data)
        return data if success else None
    except Exception as e:
        print_result("Get User by Clerk ID", False, {"error": str(e)})
        return None

def test_create_device_with_user():
    """Test POST /api/devices with user header"""
    print_section("TEST 4: Create Device with User Association")
    
    url = f"{BASE_URL}/api/devices"
    headers = {
        "X-Clerk-User-Id": TEST_CLERK_USER_ID,
        "Content-Type": "application/json"
    }
    payload = {
        "imei": TEST_IMEI,
        "name": "Test GPS Tracker",
        "description": "Automated test device"
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        success = response.status_code == 201
        data = response.json() if success else {"error": response.text}
        print_result("Create Device with User", success, data)
        return data if success else None
    except Exception as e:
        print_result("Create Device with User", False, {"error": str(e)})
        return None

def test_list_devices_filtered():
    """Test GET /api/devices with user filter"""
    print_section("TEST 5: List Devices (Filtered by User)")
    
    url = f"{BASE_URL}/api/devices"
    headers = {
        "X-Clerk-User-Id": TEST_CLERK_USER_ID
    }
    
    try:
        response = requests.get(url, headers=headers)
        success = response.status_code == 200
        data = response.json() if success else {"error": response.text}
        
        if success:
            print(f"\nFound {len(data)} device(s) for user {TEST_CLERK_USER_ID}")
        
        print_result("List User's Devices", success, data)
        return data if success else None
    except Exception as e:
        print_result("List User's Devices", False, {"error": str(e)})
        return None

def test_list_devices_unfiltered():
    """Test GET /api/devices without user filter"""
    print_section("TEST 6: List All Devices (No Filter)")
    
    url = f"{BASE_URL}/api/devices"
    
    try:
        response = requests.get(url)
        success = response.status_code == 200
        data = response.json() if success else {"error": response.text}
        
        if success:
            print(f"\nFound {len(data)} total device(s) in system")
        
        print_result("List All Devices", success, {"device_count": len(data) if success else 0})
        return data if success else None
    except Exception as e:
        print_result("List All Devices", False, {"error": str(e)})
        return None

def test_assign_device(device_id):
    """Test POST /api/devices/{device_id}/assign"""
    print_section("TEST 7: Assign Existing Device to User")
    
    url = f"{BASE_URL}/api/devices/{device_id}/assign"
    headers = {
        "X-Clerk-User-Id": TEST_CLERK_USER_ID
    }
    
    try:
        response = requests.post(url, headers=headers)
        success = response.status_code == 200
        data = response.json() if success else {"error": response.text}
        print_result("Assign Device to User", success, data)
        return data if success else None
    except Exception as e:
        print_result("Assign Device to User", False, {"error": str(e)})
        return None

def test_missing_fields():
    """Test validation - missing required fields"""
    print_section("TEST 8: Validation - Missing Required Fields")
    
    url = f"{BASE_URL}/api/auth/sync"
    payload = {
        "email": "test@example.com"
        # Missing clerk_user_id
    }
    
    try:
        response = requests.post(url, json=payload)
        success = response.status_code == 422  # Validation error expected
        data = response.json() if response.text else {"error": "No response"}
        print_result("Missing clerk_user_id Validation", success, data)
    except Exception as e:
        print_result("Missing clerk_user_id Validation", False, {"error": str(e)})

def test_invalid_clerk_user_id():
    """Test with invalid clerk user ID"""
    print_section("TEST 9: Invalid Clerk User ID")
    
    url = f"{BASE_URL}/api/devices"
    headers = {
        "X-Clerk-User-Id": "invalid_user_that_does_not_exist"
    }
    
    try:
        response = requests.get(url, headers=headers)
        success = response.status_code == 200  # Should return empty list
        data = response.json() if success else {"error": response.text}
        
        if success and len(data) == 0:
            print_result("Returns empty list for invalid user", True, {"message": "Correctly returns empty list"})
        else:
            print_result("Returns empty list for invalid user", False, data)
    except Exception as e:
        print_result("Returns empty list for invalid user", False, {"error": str(e)})

def run_all_tests():
    """Run all tests in sequence"""
    print("\n" + "🚀" * 30)
    print("  CLERK AUTHENTICATION INTEGRATION TEST SUITE")
    print("🚀" * 30)
    print(f"\nBase URL: {BASE_URL}")
    print(f"Test Clerk User ID: {TEST_CLERK_USER_ID}")
    print(f"Test Email: {TEST_EMAIL}")
    print(f"Test IMEI: {TEST_IMEI}")
    
    # Test 1: Create user
    user_data = test_user_sync()
    if not user_data:
        print("\n❌ User sync failed. Cannot continue with tests.")
        return
    
    # Test 2: Update user
    updated_user = test_user_sync_update()
    
    # Test 3: Get user
    test_get_user()
    
    # Test 4: Create device with user
    device_data = test_create_device_with_user()
    
    # Test 5: List devices (filtered)
    test_list_devices_filtered()
    
    # Test 6: List all devices (unfiltered)
    all_devices = test_list_devices_unfiltered()
    
    # Test 7: Assign device (if we have an unassigned device)
    if all_devices and len(all_devices) > 0:
        # Find a device without user_id or create scenario
        unassigned = next((d for d in all_devices if d.get('user_id') is None), None)
        if unassigned:
            test_assign_device(unassigned['id'])
        else:
            print_section("TEST 7: Assign Existing Device to User")
            print("\nℹ️  SKIP - No unassigned devices found")
    
    # Test 8: Validation
    test_missing_fields()
    
    # Test 9: Invalid user ID
    test_invalid_clerk_user_id()
    
    # Summary
    print_section("TEST SUMMARY")
    print("""
✅ All core functionality has been tested:
   - User sync (create/update)
   - User retrieval
   - Device creation with user association
   - Device listing with user filtering
   - Device assignment
   - Validation and error handling

📝 Next Steps:
   1. Review the test results above
   2. Check that all tests passed (✅)
   3. Update mobile app to use these endpoints
   4. Add X-Clerk-User-Id header to all API requests
   5. Deploy to production server

🔗 API Documentation:
   - Swagger UI: {}/docs
   - ReDoc: {}/redoc

For more details, see: server/CLERK_AUTH_IMPLEMENTATION.md
    """.format(BASE_URL, BASE_URL))

if __name__ == "__main__":
    try:
        run_all_tests()
    except KeyboardInterrupt:
        print("\n\n⚠️  Tests interrupted by user")
    except Exception as e:
        print(f"\n\n❌ Unexpected error: {str(e)}")
        import traceback
        traceback.print_exc()

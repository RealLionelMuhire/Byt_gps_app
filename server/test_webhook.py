import asyncio
import time
import json
import hmac
import hashlib
import base64
from fastapi import Request

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.user import User
from app.api.webhooks import clerk_webhook, handle_user_deleted

# Valid base64 secret for testing
TEST_SECRET = "whsec_MjIzNDU2Nzg5MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTI="
settings.CLERK_WEBHOOK_SECRET = TEST_SECRET

def setup_test_user():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.clerk_user_id == "user_test123").first()
        if not user:
            user = User(
                clerk_user_id="user_test123",
                email="webhooktest@example.com",
                first_name="Webhook",
                last_name="Test",
                role="USER"
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f"✅ Setup: Created test user with ID {user.id}")
        else:
            print(f"✅ Setup: Test user already exists with ID {user.id}")
        return user.clerk_user_id
    finally:
        db.close()

def check_user_deleted():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.clerk_user_id == "user_test123").first()
        if user:
            print(f"❌ Verification Failed: User user_test123 still exists in DB!")
            return False
        else:
            print(f"✅ Verification Success: User user_test123 was successfully deleted from DB!")
            return True
    finally:
        db.close()

def sign_svix_payload(secret: str, msg_id: str, timestamp: str, payload: str) -> str:
    secret_bytes = base64.b64decode(secret.split("_")[1])
    to_sign = f"{msg_id}.{timestamp}.{payload}".encode("utf-8")
    signature = hmac.new(secret_bytes, to_sign, hashlib.sha256).digest()
    return base64.b64encode(signature).decode("utf-8")

class MockRequest:
    def __init__(self, body_bytes: bytes):
        self._body = body_bytes
    
    async def body(self):
        return self._body

async def test_webhook():
    print("Starting Webhook Integration Test...")
    clerk_user_id = setup_test_user()
    
    # Construct Payload
    payload_dict = {
        "data": {
            "id": clerk_user_id,
            "deleted": True
        },
        "object": "event",
        "type": "user.deleted"
    }
    payload_str = json.dumps(payload_dict)
    
    msg_id = "msg_2QZ9mBw9k9QZ9mBw9k9QZ9mBw9k"
    timestamp = str(int(time.time()))
    
    # Generate Svix Signature
    signature = sign_svix_payload(TEST_SECRET, msg_id, timestamp, payload_str)
    signature_header = f"v1,{signature}"
    
    print(f"Generated Payload: {payload_str}")
    print(f"Generated Signature: {signature_header}")
    
    # Call Webhook Handler
    request = MockRequest(payload_str.encode("utf-8"))
    db = SessionLocal()
    try:
        response = await clerk_webhook(
            request=request,
            svix_id=msg_id,
            svix_timestamp=timestamp,
            svix_signature=signature_header,
            db=db
        )
        print(f"Webhook Response: {response}")
    except Exception as e:
        print(f"❌ Webhook Handler Exception: {e}")
    finally:
        db.close()
        
    check_user_deleted()

if __name__ == "__main__":
    asyncio.run(test_webhook())

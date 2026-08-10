"""
Clerk Webhooks

This module handles real-time webhooks from Clerk to synchronize user deletions.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request, Header
from sqlalchemy.orm import Session
from svix.webhooks import Webhook, WebhookVerificationError

from app.core.config import settings
from app.core.database import get_db
from app.models.user import User
from app.models.device import Device
from app.models.vehicle import Vehicle
from app.models.subscription import Subscription, Payment
from app.models.trip import Trip
from app.api.auth import claim_pending_client_user

logger = logging.getLogger(__name__)
router = APIRouter()

@router.post("/clerk", status_code=200)
async def clerk_webhook(
    request: Request,
    svix_id: str = Header(None, alias="svix-id"),
    svix_timestamp: str = Header(None, alias="svix-timestamp"),
    svix_signature: str = Header(None, alias="svix-signature"),
    db: Session = Depends(get_db)
):
    """
    Handle Clerk Webhooks
    Verified using Svix standard.
    """
    if not settings.CLERK_WEBHOOK_SECRET:
        logger.error("CLERK_WEBHOOK_SECRET is not configured.")
        raise HTTPException(status_code=500, detail="Server not configured for webhooks.")

    if not svix_id or not svix_timestamp or not svix_signature:
        raise HTTPException(status_code=400, detail="Missing svix headers")

    payload = await request.body()
    
    headers = {
        "svix-id": svix_id,
        "svix-timestamp": svix_timestamp,
        "svix-signature": svix_signature,
    }

    try:
        wh = Webhook(settings.CLERK_WEBHOOK_SECRET)
        event = wh.verify(payload, headers)
    except WebhookVerificationError as e:
        logger.error(f"Invalid webhook signature: {e}")
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = event.get("type")
    data = event.get("data", {})

    logger.info(f"Received Clerk Webhook: {event_type}")

    if event_type == "user.deleted":
        clerk_user_id = data.get("id")
        if clerk_user_id:
            await handle_user_deleted(clerk_user_id, db)
    elif event_type in ("user.created", "user.updated"):
        await handle_user_upsert(data, db)

    return {"status": "success"}

async def handle_user_upsert(data: dict, db: Session):
    """
    Handle the creation or update of a user from Clerk.
    Upserts the user in the database.
    """
    from app.models.user import Role
    from datetime import datetime

    clerk_user_id = data.get("id")
    if not clerk_user_id:
        return

    # Extract email
    email_addresses = data.get("email_addresses", [])
    primary_email_id = data.get("primary_email_address_id")
    
    email = None
    if primary_email_id:
        for ea in email_addresses:
            if ea.get("id") == primary_email_id:
                email = ea.get("email_address")
                break
    
    if not email and email_addresses:
        email = email_addresses[0].get("email_address")
        
    if not email:
        logger.warning(f"Webhook {data.get('id')} has no email address.")
        return

    first_name = data.get("first_name") or "Unknown"
    last_name = data.get("last_name") or "Unknown"

    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()

    # Adopt a pre-provisioned pending-invitation row (created by the admin
    # dashboard) once the invited client actually signs up — this preserves
    # any device assignments made against the pending row.
    if user is None:
        user = claim_pending_client_user(
            db,
            clerk_user_id=clerk_user_id,
            email=email,
            first_name=first_name,
            last_name=last_name,
        )

    try:
        if user:
            # Update
            logger.info(f"Webhook updating user {clerk_user_id}")
            user.email = email
            user.first_name = first_name
            user.last_name = last_name
            user.updated_at = datetime.utcnow()
        else:
            # Create
            is_first_user = db.query(User).count() == 0
            initial_role = Role.SUPER_ADMIN if is_first_user else Role.USER
            
            logger.info(f"Webhook creating user {clerk_user_id} with role {initial_role.value}")
            user = User(
                clerk_user_id=clerk_user_id,
                email=email,
                first_name=first_name,
                last_name=last_name,
                role=initial_role,
                onboarding_complete=is_first_user,
                onboarding_step=9 if is_first_user else 0,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            db.add(user)
        
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error during user upsert for {clerk_user_id}: {e}")
        raise HTTPException(status_code=500, detail="Database error during upsert")


async def handle_user_deleted(clerk_user_id: str, db: Session):
    """
    Handle the deletion of a user from Clerk.
    Removes the user from the database and frees up their devices.
    """
    user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
    if not user:
        logger.info(f"Webhook user.deleted received for {clerk_user_id}, but not found in DB.")
        return

    logger.info(f"Deleting user {user.id} ({user.email}) due to Clerk webhook.")

    try:
        # Free up the user's devices so they can be paired by someone else
        devices = db.query(Device).filter(Device.user_id == user.id).all()
        for device in devices:
            device.user_id = None
            # 'in_stock', not 'registered' — this device already proved TCP
            # connectivity to get to 'sold' in the first place; only its
            # ownership is being cleared. 'registered' would wrongly claim
            # it has never connected (see Device model's transition table:
            # sold -> in_stock on customer removal, never sold -> registered).
            device.lifecycle = "in_stock"
            logger.info(f"Freed device {device.imei} from deleted user {user.id}.")

        # Delete vehicles belonging to this user
        db.query(Vehicle).filter(Vehicle.clerk_user_id == clerk_user_id).delete(synchronize_session=False)

        # Delete subscriptions and payments
        db.query(Subscription).filter(Subscription.clerk_user_id == clerk_user_id).delete(synchronize_session=False)
        db.query(Payment).filter(Payment.clerk_user_id == clerk_user_id).delete(synchronize_session=False)

        # Finally, delete the user
        db.delete(user)
        db.commit()
        logger.info(f"Successfully fully deleted user {clerk_user_id} and freed devices.")

    except Exception as e:
        db.rollback()
        logger.error(f"Error while deleting user {clerk_user_id}: {e}")
        raise HTTPException(status_code=500, detail="Database error during deletion")

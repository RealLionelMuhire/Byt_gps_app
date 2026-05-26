"""Authentication API endpoints - Clerk user sync"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header
import httpx
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime
from pydantic import BaseModel, EmailStr
import logging

from app.core.database import get_db
from app.core.config import settings
from app.core.auth import require_auth
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()


# Pydantic schemas
class UserSyncRequest(BaseModel):
    """Request body for user sync"""
    clerk_user_id: str
    email: EmailStr
    name: Optional[str] = None
    
    class Config:
        json_schema_extra = {
            "example": {
                "clerk_user_id": "user_2abc123xyz456def",
                "email": "user@example.com",
                "name": "John Doe"
            }
        }


class AdminCreateUserRequest(BaseModel):
    """Request body for creating a user via admin endpoint"""
    email: EmailStr
    password: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "email": "user@example.com",
                "password": "StrongPassword123!",
                "first_name": "John",
                "last_name": "Doe"
            }
        }


class UserResponse(BaseModel):
    """User response model"""
    id: int
    clerk_user_id: str
    email: str
    name: Optional[str]
    is_admin: bool
    onboarding_step: Optional[int] = 0
    onboarding_complete: Optional[bool] = False
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


@router.post("/sync", response_model=UserResponse, status_code=200)
async def sync_user(
    user_data: UserSyncRequest,
    db: Session = Depends(get_db)
):
    """
    Sync Clerk-authenticated user to database (upsert operation)
    
    This endpoint creates or updates user records based on Clerk authentication data.
    It's called automatically when users sign in to the mobile app.
    
    - **clerk_user_id**: Unique identifier from Clerk authentication (required)
    - **email**: User's email address (required)
    - **name**: User's full name (optional)
    
    Returns the created or updated user record with timestamps.
    """
    try:
        # Validate required fields
        if not user_data.clerk_user_id or not user_data.email:
            raise HTTPException(
                status_code=400,
                detail="Missing required fields: clerk_user_id and email are required"
            )
        
        # Try to find existing user by clerk_user_id
        user = db.query(User).filter(
            User.clerk_user_id == user_data.clerk_user_id
        ).first()
        
        if user:
            # Update existing user
            logger.info(f"Updating existing user: {user_data.clerk_user_id}")
            user.email = user_data.email
            user.name = user_data.name
            user.updated_at = datetime.utcnow()
        else:
            # Create new user
            is_first_user = db.query(User).count() == 0
            
            logger.info(f"Creating new user: {user_data.clerk_user_id}. First user admin: {is_first_user}")
            user = User(
                clerk_user_id=user_data.clerk_user_id,
                email=user_data.email,
                name=user_data.name,
                is_admin=is_first_user,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            db.add(user)
        
        # Commit changes
        db.commit()
        db.refresh(user)
        
        logger.info(f"User synced successfully: {user.clerk_user_id} (ID: {user.id})")
        return user
        
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except SQLAlchemyError as e:
        # Handle database errors
        db.rollback()
        logger.error(f"Database error during user sync: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="Database error occurred"
        )
    except Exception as e:
        # Handle unexpected errors
        db.rollback()
        logger.error(f"Unexpected error during user sync: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred"
        )


@router.get("/user/{clerk_user_id}", response_model=UserResponse)
async def get_user(
    clerk_user_id: str,
    db: Session = Depends(get_db)
):
    """
    Get user by Clerk user ID
    
    - **clerk_user_id**: Clerk user identifier
    """
    user = db.query(User).filter(
        User.clerk_user_id == clerk_user_id
    ).first()
    
    if not user:
        raise HTTPException(
            status_code=404,
            detail=f"User not found: {clerk_user_id}"
        )
    
    return user


@router.get("/me", response_model=UserResponse)
async def get_current_user(
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db)
):
    """
    Get current authenticated user profile
    """
    user = db.query(User).filter(
        User.clerk_user_id == clerk_user_id
    ).first()
    
    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )
    
    return user



@router.post("/admin-create-user", response_model=UserResponse, status_code=201)
async def admin_create_user(
    user_data: AdminCreateUserRequest,
    x_admin_secret: Optional[str] = Header(None),
    db: Session = Depends(get_db)
):
    """
    Create a new user in Clerk and sync to local database.
    Requires ADMIN_SECRET in headers and CLERK_SECRET_KEY in environment.
    """
    # 1. Security Check
    if not settings.ADMIN_SECRET:
        logger.warning("ADMIN_SECRET is not set. Admin user creation endpoint is unsecured!")
    elif x_admin_secret != settings.ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid admin secret")

    if not settings.CLERK_SECRET_KEY:
        raise HTTPException(status_code=500, detail="CLERK_SECRET_KEY is not configured on the server")

    # 2. Create user in Clerk
    try:
        async with httpx.AsyncClient() as client:
            payload = {
                "email_address": [user_data.email],
                "password": user_data.password,
            }
            if user_data.first_name:
                payload["first_name"] = user_data.first_name
            if user_data.last_name:
                payload["last_name"] = user_data.last_name

            response = await client.post(
                "https://api.clerk.com/v1/users",
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.CLERK_SECRET_KEY}",
                    "Content-Type": "application/json"
                },
                timeout=10.0
            )
            
            if response.status_code != 200:
                logger.error(f"Clerk API error: {response.text}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to create user in Clerk: {response.text}"
                )
            
            clerk_data = response.json()
            clerk_user_id = clerk_data.get("id")
            
            if not clerk_user_id:
                raise HTTPException(status_code=500, detail="Clerk API did not return an ID")

    except httpx.RequestError as e:
        logger.error(f"Request to Clerk API failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to communicate with Clerk API")

    # 3. Sync to local database
    try:
        # Check if user already exists in DB (just in case)
        existing_user = db.query(User).filter(User.clerk_user_id == clerk_user_id).first()
        if existing_user:
            return existing_user

        full_name = None
        if user_data.first_name or user_data.last_name:
            parts = [p for p in (user_data.first_name, user_data.last_name) if p]
            full_name = " ".join(parts)

        is_first_user = db.query(User).count() == 0

        user = User(
            clerk_user_id=clerk_user_id,
            email=user_data.email,
            name=full_name,
            is_admin=is_first_user,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        logger.info(f"Admin created new user successfully: {user.clerk_user_id} (ID: {user.id})")
        return user

    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Database error during admin user creation: {str(e)}")
        raise HTTPException(status_code=500, detail="Database error occurred")

"""Authentication API endpoints - Clerk user sync"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from datetime import datetime
from pydantic import BaseModel, EmailStr
import logging

from app.core.database import get_db
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


class UserResponse(BaseModel):
    """User response model"""
    id: int
    clerk_user_id: str
    email: str
    name: Optional[str]
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
            logger.info(f"Creating new user: {user_data.clerk_user_id}")
            user = User(
                clerk_user_id=user_data.clerk_user_id,
                email=user_data.email,
                name=user_data.name,
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

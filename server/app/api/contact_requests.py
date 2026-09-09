"""Contact request API — "order a GPS device" and "support" requests
raised from the mobile app, surfaced to admins for follow-up."""

import logging
from datetime import datetime
from typing import List, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import require_auth, require_admin
from app.models.contact_request import ContactRequest
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()


class ContactRequestCreate(BaseModel):
    type: Literal["order_gps", "support"]
    name: str
    phone: str
    message: str


class ContactRequestResponse(BaseModel):
    id: int
    clerk_user_id: str
    type: str
    name: str
    phone: str
    message: str
    created_at: datetime

    class Config:
        from_attributes = True


@router.post("", response_model=ContactRequestResponse, status_code=201)
async def create_contact_request(
    body: ContactRequestCreate,
    clerk_user_id: str = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Record a contact request from the authenticated user."""
    request = ContactRequest(
        clerk_user_id=clerk_user_id,
        type=body.type,
        name=body.name,
        phone=body.phone,
        message=body.message,
    )
    db.add(request)
    db.commit()
    db.refresh(request)

    logger.info("Contact request created id=%d type=%s user=%s", request.id, request.type, clerk_user_id)
    return request


@router.get("", response_model=List[ContactRequestResponse])
async def list_contact_requests(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """List contact requests, newest first. Requires ADMIN or SUPER_ADMIN role."""
    requests = (
        db.query(ContactRequest)
        .order_by(ContactRequest.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return requests

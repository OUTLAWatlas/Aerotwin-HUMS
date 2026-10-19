from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    Principal, create_access_token, create_refresh_token, get_current_principal,
    refresh_access_token, verify_password,
)
from app.db.models import User
from app.db.session import get_db
from app.schemas.api_models import LoginRequest, MeResponse, RefreshRequest, TokenResponse

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.user_id == payload.user_id))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return TokenResponse(
        access_token=create_access_token(user.user_id, user.role),
        refresh_token=create_refresh_token(user.user_id, user.role),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest):
    new_access = refresh_access_token(payload.refresh_token)
    return TokenResponse(access_token=new_access, refresh_token=payload.refresh_token)


@router.get("/me", response_model=MeResponse)
async def me(principal: Principal = Depends(get_current_principal)):
    """Restores a session from a stored access token (e.g. on a dashboard
    page refresh) without requiring the user to log in again. Added for the
    AeroTwin-HUMS dashboard's AuthContext, which calls this on mount."""
    return MeResponse(user_id=principal.user_id, role=principal.role)

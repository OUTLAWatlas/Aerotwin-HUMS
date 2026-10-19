"""
JWT authentication + RBAC enforcement.

TLS is NOT handled here -- it is terminated by uvicorn (see main.py, run with
ssl_certfile/ssl_keyfile) or by an upstream ingress/load balancer in production.
This module covers the other two pillars: JWT auth and RBAC.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import yaml
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

from app.core.config import get_settings

settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


class TokenPayload(BaseModel):
    sub: str
    role: str
    jti: str
    exp: datetime
    token_type: str  # "access" | "refresh"


class Principal(BaseModel):
    user_id: str
    role: str


# --------------------------------------------------------------------------- #
# Password / token helpers
# --------------------------------------------------------------------------- #
def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def _create_token(subject: str, role: str, expires_delta: timedelta, token_type: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "role": role,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + expires_delta,
        "token_type": token_type,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(subject: str, role: str) -> str:
    return _create_token(
        subject, role, timedelta(minutes=settings.jwt_access_token_expire_minutes), "access"
    )


def create_refresh_token(subject: str, role: str) -> str:
    return _create_token(
        subject, role, timedelta(days=settings.jwt_refresh_token_expire_days), "refresh"
    )


def decode_token(token: str) -> TokenPayload:
    try:
        raw = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        return TokenPayload(**raw)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def refresh_access_token(refresh_token: str) -> str:
    payload = decode_token(refresh_token)
    if payload.token_type != "refresh":
        raise HTTPException(status_code=401, detail="Not a refresh token")
    return create_access_token(payload.sub, payload.role)


# --------------------------------------------------------------------------- #
# Dependency: current principal
# --------------------------------------------------------------------------- #
def get_current_principal(token: str = Depends(oauth2_scheme)) -> Principal:
    payload = decode_token(token)
    if payload.token_type != "access":
        raise HTTPException(status_code=401, detail="Access token required")
    return Principal(user_id=payload.sub, role=payload.role)


# --------------------------------------------------------------------------- #
# RBAC
# --------------------------------------------------------------------------- #
class RBACPolicy:
    """Loads config/roles.yaml and answers permission checks."""

    def __init__(self, path):
        with open(path) as f:
            self._raw = yaml.safe_load(f)
        self.permissions: dict[str, list[str]] = self._raw.get("roles", {})

    def is_allowed(self, role: str, permission: str) -> bool:
        role_perms = self.permissions.get(role, [])
        return "*" in role_perms or permission in role_perms


_rbac_policy: Optional[RBACPolicy] = None


def get_rbac_policy() -> RBACPolicy:
    global _rbac_policy
    if _rbac_policy is None:
        _rbac_policy = RBACPolicy(settings.rbac_config_path)
    return _rbac_policy


def require_permission(permission: str):
    """FastAPI dependency factory: require_permission('predictions:write')."""

    def _checker(principal: Principal = Depends(get_current_principal)) -> Principal:
        policy = get_rbac_policy()
        if not policy.is_allowed(principal.role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{principal.role}' lacks permission '{permission}'",
            )
        return principal

    return _checker

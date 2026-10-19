"""
Audit logging: every API call, data access, config change, and model
inference is recorded with user id, timestamp, action, resource, and outcome.
Stored in a dedicated `audit_log` hypertable, separate from operational data.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.db.session import get_sessionmaker
from app.db.models import AuditLog


class AuditLogMiddleware(BaseHTTPMiddleware):
    """Wraps every request and writes an audit_log row, independent of the
    route's own business logic. Route handlers can add extra context via
    `request.state.audit_resource` / `request.state.audit_action`.
    """

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        request.state.audit_resource = request.url.path
        request.state.audit_action = request.method
        request.state.audit_drone_id = None
        request.state.audit_engine_id = None
        outcome = "success"
        status_code = 500
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            if status_code >= 400:
                outcome = "failure"
            return response
        except Exception:
            outcome = "error"
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            user_id = getattr(request.state, "user_id", "anonymous")
            await self._write_log(
                user_id=user_id,
                action=request.state.audit_action,
                resource=request.state.audit_resource,
                drone_id=getattr(request.state, "audit_drone_id", None),
                engine_id=getattr(request.state, "audit_engine_id", None),
                outcome=outcome,
                status_code=status_code,
                duration_ms=duration_ms,
            )

    @staticmethod
    async def _write_log(user_id, action, resource, drone_id, engine_id,
                          outcome, status_code, duration_ms):
        Session = get_sessionmaker()
        async with Session() as session:
            now = datetime.now(timezone.utc)
            entry = AuditLog(
                id=uuid.uuid4(),
                user_id=user_id,
                action=action,
                resource=resource,
                drone_id=drone_id,
                engine_id=engine_id,
                outcome=outcome,
                status_code=status_code,
                duration_ms=duration_ms,
                created_at=now,
                time=now,
            )
            session.add(entry)
            await session.commit()

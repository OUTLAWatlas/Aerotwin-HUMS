from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerting.service import AlertCandidate, alerting_service
from app.core.security import require_permission
from app.db.models import Alert
from app.db.session import get_db
from app.schemas.api_models import AlertOut

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertOut])
async def list_alerts(
    request: Request,
    mission_id: str | None = None,
    drone_id: str | None = None,
    engine_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    principal=Depends(require_permission("alerts:read")),
):
    request.state.user_id = principal.user_id
    query = select(Alert).where(Alert.status == "open").order_by(Alert.created_at.desc())
    if mission_id:
        query = query.where(Alert.mission_id == mission_id)
    if drone_id:
        query = query.where(Alert.drone_id == drone_id)
    if engine_id:
        query = query.where(Alert.engine_id == engine_id)
    result = await db.execute(query.limit(200))
    rows = result.scalars().all()

    payloads = [
        AlertOut(
            alert_id=str(row.id),
            mission_id=row.mission_id,
            drone_id=row.drone_id,
            engine_id=row.engine_id,
            drone_type=row.drone_type,
            timestamp=row.created_at.isoformat(),
            severity=row.severity,
            description=row.description,
            recommendation=row.recommendation,
            confidence=row.confidence,
        )
        for row in rows
    ]
    # Ranked by severity for frontend consumption
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return sorted(payloads, key=lambda a: order.get(a.severity, 99))


@router.post("/{alert_id}/ack")
async def acknowledge_alert(
    alert_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal=Depends(require_permission("alerts:acknowledge")),
):
    request.state.user_id = principal.user_id
    alert = await db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.status = "acknowledged"
    alert.acknowledged_by = principal.user_id
    alert.acknowledged_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "acknowledged", "alert_id": alert_id}

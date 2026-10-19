"""
HTTP ingestion endpoint -- a convenience/fallback path for a single one-off
reading (e.g. a ground-based test rig sending an occasional value), or for
backfilling historical readings. It writes straight to the SensorReading
table and does NOT run orchestration/alerting -- it's for storage, not live
processing.

For live, continuously-streamed telemetry that should actually drive
predictions/alerts/dashboard updates, use `/ws/ingest`
(app/api/routes_ws.py) instead, which runs each reading through the full
orchestration pipeline. The CAN and MQTT adapters in app/ingestion/ are an
alternative live path for real hardware, but aren't started by default --
see their docstrings.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import require_permission
from app.db.models import SensorReading
from app.db.session import get_db
from app.ingestion.base import NormalizedReading, ValidationError, validate_reading
from app.mission.drone_registry import get_drone_registry
from app.schemas.api_models import IngestReadingRequest

router = APIRouter(prefix="/api/v1/ingest", tags=["ingestion"])
settings = get_settings()


@router.post("")
async def ingest_reading(
    payload: IngestReadingRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal=Depends(require_permission("ingestion:write")),
):
    request.state.user_id = principal.user_id
    request.state.audit_drone_id = payload.drone_id
    request.state.audit_engine_id = payload.engine_id

    drone_registry = get_drone_registry(settings.drone_config_path)
    engine_info = drone_registry.get_engine_info(payload.engine_id)
    if engine_info is None or engine_info.drone_id != payload.drone_id:
        raise HTTPException(
            status_code=422,
            detail=f"engine_id '{payload.engine_id}' is not a configured engine on drone_id '{payload.drone_id}'",
        )

    try:
        reading = NormalizedReading(
            mission_id=payload.mission_id,
            drone_id=payload.drone_id,
            engine_id=payload.engine_id,
            drone_type=engine_info.drone_type,
            sensor_id=payload.sensor_id,
            sensor_type=payload.sensor_type,
            value=payload.value,
            unit=payload.unit,
            timestamp=datetime.fromisoformat(payload.timestamp.replace("Z", "+00:00")),
            sequence_no=payload.sequence_no,
            source_protocol="http",
        )
        validate_reading(reading)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    db.add(SensorReading(
        mission_id=reading.mission_id,
        drone_id=reading.drone_id,
        engine_id=reading.engine_id,
        drone_type=reading.drone_type,
        sensor_id=reading.sensor_id,
        sensor_type=reading.sensor_type,
        value=reading.value,
        unit=reading.unit,
        source_protocol=reading.source_protocol,
        sequence_no=reading.sequence_no,
        time=reading.timestamp,
    ))
    await db.commit()
    return {"status": "accepted", "drone_id": reading.drone_id, "engine_id": reading.engine_id}

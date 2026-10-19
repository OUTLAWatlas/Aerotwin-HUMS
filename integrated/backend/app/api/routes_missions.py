from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_permission
from app.db.session import get_db
from app.mission.drone_registry import get_drone_registry
from app.mission.manager import MissionManager
from app.models_serving.registry import model_registry
from app.schemas.api_models import EngineHealthComparisonOut, MissionCreateRequest, MissionOut
from app.core.config import get_settings

router = APIRouter(prefix="/api/v1/missions", tags=["missions"])
settings = get_settings()


@router.post("", response_model=MissionOut)
async def create_or_update_mission(
    payload: MissionCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal=Depends(require_permission("missions:write")),
):
    request.state.user_id = principal.user_id
    request.state.audit_drone_id = payload.drone_id
    manager = MissionManager(db)
    try:
        mission = await manager.create_or_update_mission(
            mission_id=payload.mission_id,
            drone_id=payload.drone_id,
            flight_duration_planned_min=payload.flight_duration_planned_min,
            operating_conditions=payload.operating_conditions,
            risk_tolerance=payload.risk_tolerance,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return MissionOut(**{k: getattr(mission, k) for k in MissionOut.model_fields})


@router.get("/{mission_id}", response_model=MissionOut)
async def get_mission(
    mission_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal=Depends(require_permission("missions:read")),
):
    request.state.user_id = principal.user_id
    manager = MissionManager(db)
    mission = await manager.get_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    request.state.audit_drone_id = mission.drone_id
    return MissionOut(**{k: getattr(mission, k) for k in MissionOut.model_fields})


@router.get("/{mission_id}/engines/compare", response_model=list[EngineHealthComparisonOut])
async def compare_engines(
    mission_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal=Depends(require_permission("missions:read")),
):
    """Multi-engine correlation: for a drone with more than one engine (the
    dual_engine configuration), returns each engine's latest health index
    side by side so a dashboard can spot one engine degrading relative to
    its twin. For single_engine drones this returns a single-item list."""
    request.state.user_id = principal.user_id
    manager = MissionManager(db)
    mission = await manager.get_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    request.state.audit_drone_id = mission.drone_id

    comparison = await manager.compare_engine_health(mission.drone_id)
    return [EngineHealthComparisonOut(**c) for c in comparison]


@router.post("/{mission_id}/close")
async def close_mission(
    mission_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal=Depends(require_permission("missions:write")),
):
    """Marks the mission completed and releases any open AeroTwin sessions
    for its engines. Without this, a stateful session (Kalman drift
    estimator, AR noise filter, residual history) sits alive until its idle
    TTL expires -- fine occasionally, wasteful for a fleet flying all day."""
    request.state.user_id = principal.user_id
    manager = MissionManager(db)
    mission = await manager.get_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    request.state.audit_drone_id = mission.drone_id

    drone_registry = get_drone_registry(settings.drone_config_path)
    aerotwin_model = model_registry.get("aerotwin_physics_v1")
    closed_sessions = []
    if aerotwin_model is not None:
        for engine_id in drone_registry.get_engines_for_drone(mission.drone_id):
            summary = aerotwin_model.close_session(mission_id, engine_id)
            if summary is not None:
                closed_sessions.append({"engine_id": engine_id, **summary})

    mission.status = "completed"
    await db.commit()
    return {"mission_id": mission_id, "status": "completed", "closed_sessions": closed_sessions}

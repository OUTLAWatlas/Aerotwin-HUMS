import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.alerting.service import AlertCandidate, alerting_service
from app.core.config import get_settings
from app.core.security import require_permission
from app.db.models import Alert, Anomaly, FaultDiagnosis, HealthIndex, RULPrediction
from app.db.session import get_db
from app.mission.drone_registry import get_drone_registry
from app.mission.manager import MissionManager
from app.models_serving.registry import model_registry
from app.orchestration.router import OrchestrationEngine
from app.realtime import bridge
from app.realtime.connection_manager import connection_manager
from app.schemas.api_models import ModelCallResultOut, PredictionRequest, PredictionResponse

router = APIRouter(prefix="/api/v1/predict", tags=["predictions"])
settings = get_settings()


def get_orchestration_engine() -> OrchestrationEngine:
    return OrchestrationEngine(registry=model_registry)


class UnknownEngineError(ValueError):
    """Raised when engine_id/drone_id don't match config/drone_configs.yaml.
    A plain ValueError subclass (not HTTPException) so this function is
    equally usable from an HTTP route (which converts it to a 422) and a
    WebSocket handler (which has no HTTP status codes to raise)."""


async def execute_prediction(
    payload: PredictionRequest,
    db: AsyncSession,
    engine: OrchestrationEngine,
) -> PredictionResponse:
    """The full pipeline behind POST /api/v1/predict: validate the
    engine/drone pairing, run orchestration, persist results, raise any
    resulting alerts, and broadcast live dashboard frames.

    Pulled out of the `run_prediction` route so the WebSocket ingestion
    endpoint (app/api/routes_ws.py, `/ws/ingest`) can drive the exact same
    pipeline per message, instead of requiring a live telemetry source to
    make one HTTP POST per reading. Transport-specific concerns (auth
    dependency, HTTPException vs. an error frame, audit logging) stay in
    each caller.
    """
    drone_registry = get_drone_registry(settings.drone_config_path)
    engine_info = drone_registry.get_engine_info(payload.engine_id)
    if engine_info is None or engine_info.drone_id != payload.drone_id:
        raise UnknownEngineError(
            f"engine_id '{payload.engine_id}' is not a configured engine on drone_id '{payload.drone_id}'"
        )

    mission_manager = MissionManager(db)
    risk_tier = await mission_manager.get_risk_tier(payload.mission_id)

    sensor_dict = {k: v.model_dump() for k, v in payload.sensor_data.items()}
    result = await engine.run(
        mission_id=payload.mission_id,
        drone_id=payload.drone_id,
        engine_id=payload.engine_id,
        drone_type=engine_info.drone_type,
        sensor_payload=sensor_dict,
        risk_tier=risk_tier,
        operating_point=payload.operating_point,
    )

    # Persists anomaly/fault/health/RUL rows (unchanged behavior) AND, new
    # for the dashboard integration, raises any resulting alerts and builds
    # the live WebSocket frames for this tick -- see app/realtime/bridge.py.
    frames = await _persist_results(
        db, result,
        drone_registry=drone_registry,
        sensor_dict=sensor_dict,
        operating_point=payload.operating_point,
        risk_tier=risk_tier,
    )
    # Broadcast after commit so a client never sees a frame referencing a
    # row that isn't actually persisted yet (e.g. if a GET raced it).
    await connection_manager.broadcast(payload.drone_id, frames)

    return PredictionResponse(
        mission_id=result.mission_id,
        drone_id=result.drone_id,
        engine_id=result.engine_id,
        drone_type=result.drone_type,
        rule_used=result.rule_used,
        results=[ModelCallResultOut(**r.__dict__) for r in result.results],
        total_execution_time_ms=result.total_execution_time_ms,
    )


@router.post("", response_model=PredictionResponse)
async def run_prediction(
    payload: PredictionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal=Depends(require_permission("predictions:write")),
    engine: OrchestrationEngine = Depends(get_orchestration_engine),
):
    request.state.user_id = principal.user_id
    request.state.audit_action = "run_prediction"
    request.state.audit_resource = f"mission:{payload.mission_id}"
    request.state.audit_drone_id = payload.drone_id
    request.state.audit_engine_id = payload.engine_id

    try:
        return await execute_prediction(payload, db, engine)
    except UnknownEngineError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


async def _raise_alert(db: AsyncSession, candidate: AlertCandidate, engine_idx: int) -> list[dict]:
    """Runs the candidate through the shared AlertingService (dedup +
    severity + recommendation), persists an Alert row if it wasn't
    suppressed as a duplicate, and returns the "log" frame to broadcast.
    This is the wiring that didn't exist before the dashboard integration:
    AlertingService.build_alert() was previously never called anywhere."""
    payload = alerting_service.build_alert(candidate)
    if payload is None:
        return []  # deduplicated within the alert_dedup_window -- suppressed

    db.add(Alert(
        id=uuid.UUID(payload.alert_id),
        mission_id=payload.mission_id,
        drone_id=payload.drone_id,
        engine_id=payload.engine_id,
        drone_type=payload.drone_type,
        source_type=candidate.source_type,
        source_id=uuid.UUID(candidate.source_id),
        severity=payload.severity,
        description=payload.description,
        recommendation=payload.recommendation,
        confidence=payload.confidence,
        dedup_key=payload.dedup_key,
    ))
    return [bridge.build_log_frame(engine_idx, f"{payload.description} {payload.recommendation}", payload.severity)]


async def _persist_results(
    db: AsyncSession,
    result,
    *,
    drone_registry,
    sensor_dict: dict,
    operating_point: dict | None,
    risk_tier: str,
) -> list[dict]:
    now = datetime.now(timezone.utc)
    mission_id, drone_id, engine_id, drone_type = (
        result.mission_id, result.drone_id, result.engine_id, result.drone_type,
    )
    engine_idx = bridge.engine_index(drone_registry, engine_id)

    # Telemetry + mission risk don't depend on which model ran -- they're
    # derived straight from the request and the mission's current risk tier.
    frames: list[dict] = [
        bridge.build_telemetry_frame(engine_idx, sensor_dict, operating_point=operating_point),
        bridge.build_mission_risk_frame(engine_idx, risk_tier),
    ]

    for r in result.results:
        if r.task == "combined_assessment":
            frames.extend(await _persist_aerotwin_result(
                db, mission_id, drone_id, engine_id, drone_type, r, now, engine_idx,
            ))
        elif r.task == "anomaly_detection":
            anomaly_id = uuid.uuid4()
            anomaly_detected = bool(r.output.get("anomaly_detected", False))
            severity = "high" if r.confidence > 0.7 else "medium" if r.confidence > 0.4 else "low"
            db.add(Anomaly(
                id=anomaly_id,
                mission_id=mission_id,
                drone_id=drone_id,
                engine_id=engine_id,
                drone_type=drone_type,
                sensor_id="aggregate",
                anomaly_type=str(anomaly_detected),
                severity=severity,
                confidence=r.confidence,
                model_name=r.model_name,
                model_version=r.model_version,
                detected_at=now,
                time=now,
            ))
            # Derive a simple health index from the anomaly confidence so
            # multi-engine comparison endpoints have live data to compare.
            health_score = max(0.0, 1.0 - r.confidence) if anomaly_detected else 1.0 - (r.confidence * 0.2)
            db.add(HealthIndex(
                mission_id=mission_id,
                drone_id=drone_id,
                engine_id=engine_id,
                drone_type=drone_type,
                component="engine",
                health_score=health_score,
                model_version=r.model_version,
                time=now,
            ))
            frames.append(bridge.build_anomaly_frame(engine_idx, anomaly_detected, now.isoformat()))
            frames.append(bridge.build_health_index_frame(engine_idx, health_score))
            if anomaly_detected:
                frames.extend(await _raise_alert(db, AlertCandidate(
                    mission_id=mission_id, drone_id=drone_id, engine_id=engine_id, drone_type=drone_type,
                    source_type="anomaly", source_id=str(anomaly_id), component="engine",
                    confidence=r.confidence, severity=severity,
                    description=f"Anomaly detected by {r.model_name} (confidence {r.confidence:.2f}).",
                ), engine_idx))
        elif r.task == "fault_diagnosis":
            fault_id = uuid.uuid4()
            fault_code = r.output.get("fault_code", "unknown")
            db.add(FaultDiagnosis(
                id=fault_id,
                mission_id=mission_id,
                drone_id=drone_id,
                engine_id=engine_id,
                drone_type=drone_type,
                component=fault_code,
                fault_code=fault_code,
                description=None,
                confidence=r.confidence,
                model_name=r.model_name,
                model_version=r.model_version,
                time=now,
            ))
            frames.append(bridge.build_diagnosis_frame(engine_idx, fault_code, r.confidence))
            severity = "high" if r.confidence > 0.7 else "medium" if r.confidence > 0.4 else "low"
            frames.extend(await _raise_alert(db, AlertCandidate(
                mission_id=mission_id, drone_id=drone_id, engine_id=engine_id, drone_type=drone_type,
                source_type="fault", source_id=str(fault_id), component=fault_code,
                confidence=r.confidence, severity=severity,
                description=f"Fault diagnosis: {fault_code} ({r.model_name}).",
            ), engine_idx))
        elif r.task == "rul_estimation":
            db.add(RULPrediction(
                mission_id=mission_id,
                drone_id=drone_id,
                engine_id=engine_id,
                drone_type=drone_type,
                component="aggregate",
                rul_value_hours=r.output.get("rul_hours", 0.0),
                ci_lower_hours=r.output.get("ci_lower_hours"),
                ci_upper_hours=r.output.get("ci_upper_hours"),
                confidence_level=r.confidence,
                model_name=r.model_name,
                model_version=r.model_version,
                time=now,
            ))
            frames.append(bridge.build_rul_frame(
                engine_idx, r.output.get("rul_hours"),
                r.output.get("ci_lower_hours"), r.output.get("ci_upper_hours"),
            ))
    await db.commit()
    return frames


async def _persist_aerotwin_result(db, mission_id, drone_id, engine_id, drone_type, r, now, engine_idx) -> list[dict]:
    """Unpacks AeroTwin's single combined_assessment output (see
    aerotwin_physics.service.serialise_output) into the same anomaly / fault /
    health_index / rul_predictions rows the ML/toy-physics path writes, so
    downstream consumers (alerting, dashboards) don't need to know which
    model produced them. Also returns the dashboard frames + raises alerts
    for this tick, same as the generic ML path above."""
    output = r.output
    health = output.get("health", {})
    diagnosis = output.get("diagnosis", {})
    rul = output.get("rul")
    frames: list[dict] = []

    anomaly_id = uuid.uuid4()
    anomaly_detected = bool(health.get("anomaly_flag", False))
    anomaly_score = float(health.get("anomaly_score", r.confidence))
    severity = "high" if anomaly_score > 0.7 else "medium" if anomaly_score > 0.4 else "low"

    db.add(Anomaly(
        id=anomaly_id,
        mission_id=mission_id,
        drone_id=drone_id,
        engine_id=engine_id,
        drone_type=drone_type,
        sensor_id="aerotwin_residual_vector",
        anomaly_type=str(anomaly_detected),
        severity=severity,
        confidence=anomaly_score,
        model_name=r.model_name,
        model_version=r.model_version,
        detected_at=now,
        time=now,
    ))
    frames.append(bridge.build_anomaly_frame(engine_idx, anomaly_detected, now.isoformat()))
    if anomaly_detected:
        frames.extend(await _raise_alert(db, AlertCandidate(
            mission_id=mission_id, drone_id=drone_id, engine_id=engine_id, drone_type=drone_type,
            source_type="anomaly", source_id=str(anomaly_id), component="engine",
            confidence=anomaly_score, severity=severity,
            description=f"AeroTwin anomaly detected (score {anomaly_score:.2f}).",
        ), engine_idx))

    fault_name = diagnosis.get("fault")
    # NO_FAULT is AeroTwin's own sentinel string for "nothing detected" --
    # don't write a fault_diagnoses row for the healthy case, mirroring how
    # the ML fault classifier only reports when it has something to say.
    if fault_name and fault_name not in ("none", "NO_FAULT"):
        fault_id = uuid.uuid4()
        fault_confidence = float(diagnosis.get("confidence", 0.0))
        db.add(FaultDiagnosis(
            id=fault_id,
            mission_id=mission_id,
            drone_id=drone_id,
            engine_id=engine_id,
            drone_type=drone_type,
            component="engine",
            fault_code=fault_name,
            description=diagnosis.get("explanation"),
            confidence=fault_confidence,
            model_name=r.model_name,
            model_version=r.model_version,
            time=now,
        ))
        frames.append(bridge.build_diagnosis_frame(engine_idx, fault_name, fault_confidence))
        fault_severity = "high" if fault_confidence > 0.7 else "medium" if fault_confidence > 0.4 else "low"
        frames.extend(await _raise_alert(db, AlertCandidate(
            mission_id=mission_id, drone_id=drone_id, engine_id=engine_id, drone_type=drone_type,
            source_type="fault", source_id=str(fault_id), component="engine",
            confidence=fault_confidence, severity=fault_severity,
            description=diagnosis.get("explanation") or f"AeroTwin fault diagnosis: {fault_name}.",
        ), engine_idx))
    else:
        frames.append(bridge.build_diagnosis_frame(engine_idx, None, 0.0))

    if rul is not None:
        health_index = rul.get("health_index")
        if health_index is not None:
            db.add(HealthIndex(
                mission_id=mission_id,
                drone_id=drone_id,
                engine_id=engine_id,
                drone_type=drone_type,
                component="engine",
                health_score=float(health_index),
                model_version=r.model_version,
                time=now,
            ))
            frames.append(bridge.build_health_index_frame(engine_idx, float(health_index)))
        # AeroTwin returns RUL in seconds and withholds a number below
        # min_samples_for_estimate ("no number is emitted at all" -- see
        # README §3) -- respect that by only writing a row when it did.
        if rul.get("median_s") is not None:
            median_hours = float(rul["median_s"]) / 3600.0
            lower_hours = float(rul["lower_95_s"]) / 3600.0 if rul.get("lower_95_s") is not None else None
            upper_hours = float(rul["upper_95_s"]) / 3600.0 if rul.get("upper_95_s") is not None else None
            db.add(RULPrediction(
                mission_id=mission_id,
                drone_id=drone_id,
                engine_id=engine_id,
                drone_type=drone_type,
                component="engine",
                rul_value_hours=median_hours,
                ci_lower_hours=lower_hours,
                ci_upper_hours=upper_hours,
                confidence_level=float(rul.get("confidence", 0.95)),
                model_name=r.model_name,
                model_version=r.model_version,
                time=now,
            ))
            frames.append(bridge.build_rul_frame(engine_idx, median_hours, lower_hours, upper_hours))

    return frames

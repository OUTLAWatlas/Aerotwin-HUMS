"""Live telemetry WebSockets -- outbound to the dashboard (`/ws/telemetry`)
and inbound from a sensor source (`/ws/ingest`). Frame construction for the
outbound side lives in app/realtime/bridge.py; this module is connection
lifecycle + auth for both directions.

Auth note (both endpoints): browsers/simple WS clients can't attach an
Authorization header to a WebSocket handshake, so the access token travels
as a query parameter instead. Query parameters can end up in proxy/
load-balancer access logs, so a production deployment should exchange the
access token for a short-lived, single-use WS ticket via a REST call first
rather than passing the real JWT here -- left as a follow-up; flagged
rather than silently done, since the project's REST endpoints all use the
bearer-header pattern instead.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError as PydanticValidationError

from app.api.routes_predictions import UnknownEngineError, execute_prediction
from app.core.config import get_settings
from app.core.security import decode_token, get_rbac_policy
from app.db.session import get_sessionmaker
from app.mission.drone_registry import get_drone_registry
from app.models_serving.registry import model_registry
from app.orchestration.router import OrchestrationEngine
from app.realtime.connection_manager import connection_manager
from app.schemas.api_models import PredictionRequest

router = APIRouter(tags=["realtime"])
logger = logging.getLogger(__name__)
settings = get_settings()


@router.websocket("/ws/telemetry")
async def telemetry_feed(websocket: WebSocket):
    """Outbound: the dashboard connects here to receive live frames for one
    airframe. See aerotwin-hums-dashboard.jsx's BACKEND INTEGRATION CONTRACT.

    Endpoint: GET/Upgrade /ws/telemetry?drone_id=<id>&token=<jwt access token>
    """
    drone_id = websocket.query_params.get("drone_id")
    token = websocket.query_params.get("token")

    if not drone_id or not token:
        await websocket.close(code=4400)  # 4400: application-defined "bad request"
        return

    try:
        claims = decode_token(token)
    except Exception:
        await websocket.close(code=4401)  # unauthenticated
        return
    if claims.token_type != "access":
        await websocket.close(code=4401)
        return
    if not get_rbac_policy().is_allowed(claims.role, "predictions:read"):
        await websocket.close(code=4403)  # unauthorized
        return

    drone_registry = get_drone_registry(settings.drone_config_path)
    if drone_registry.get_drone_type(drone_id) is None:
        await websocket.close(code=4404)  # unknown drone_id
        return

    await connection_manager.connect(drone_id, websocket)
    try:
        while True:
            # The dashboard's client never sends messages on this socket;
            # we just need recv() to raise WebSocketDisconnect on close so
            # we notice and clean up. A production version would also
            # answer client pings here for keepalive.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WS telemetry connection for drone_id=%s ended unexpectedly", drone_id)
    finally:
        await connection_manager.disconnect(drone_id, websocket)


@router.websocket("/ws/ingest")
async def telemetry_ingest(websocket: WebSocket):
    """Inbound: a live sensor source streams readings in over a persistent
    connection instead of POSTing to /api/v1/predict once per tick. Each
    message drives the exact same pipeline as the REST endpoint
    (`execute_prediction()` in routes_predictions.py) -- orchestration,
    persistence, alerting, and a broadcast to any `/ws/telemetry` clients
    watching that drone_id.

    This replaces the originally-planned CAN-bus ingestion path (see
    app/ingestion/can_adapter.py) for this project: python-can's socketcan
    backend only runs on Linux with a real CAN interface attached, which
    rules it out for local development on Windows/macOS entirely.
    WebSocket ingestion works anywhere a JSON-speaking client can open a
    socket -- including scripts/simulate_telemetry.py, added alongside
    this endpoint for local testing without any real hardware.
    app/ingestion/can_adapter.py and mqtt_adapter.py are left in place,
    unwired, for a future deployment that does have real hardware -- see
    those modules' docstrings for how they'd plug into this same
    `execute_prediction()` pipeline instead of the old, never-wired
    BackpressureQueue.

    Endpoint: GET/Upgrade /ws/ingest?token=<jwt access token>

    Each text frame sent by the client must be a JSON object matching
    PredictionRequest -- the same body POST /api/v1/predict takes
    (mission_id, drone_id, engine_id, sensor_data, optional
    operating_point). The server replies to each message with either:
        {"type": "ack", "mission_id": ..., "engine_id": ..., "rule_used": ...}
        {"type": "error", "detail": "..."}
    A malformed or rejected message does NOT close the connection -- only
    an auth/permission failure at connect time, or the client
    disconnecting, does.

    Permission: ingestion:write, not predictions:write -- this endpoint
    models a telemetry-emitting device/operator, not a human directly
    invoking arbitrary predictions via the REST API (compare
    config/roles.yaml: pilot/operator have ingestion:write but not
    predictions:write).

    Known gap: app/core/audit.py's AuditLogMiddleware only sees HTTP
    requests, so per-message audit trail entries aren't written here the
    way they are for POST /api/v1/predict -- only the plain `logger` calls
    below. Wiring this into the same audit table is a reasonable follow-up
    if a compliance trail is needed for the ingestion path too.
    """
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4400)
        return

    try:
        claims = decode_token(token)
    except Exception:
        await websocket.close(code=4401)
        return
    if claims.token_type != "access":
        await websocket.close(code=4401)
        return
    if not get_rbac_policy().is_allowed(claims.role, "ingestion:write"):
        await websocket.close(code=4403)
        return

    await websocket.accept()
    logger.info("WS ingest connected (user=%s)", claims.sub)

    session_factory = get_sessionmaker()
    orchestration_engine = OrchestrationEngine(registry=model_registry)

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                payload = PredictionRequest.model_validate_json(raw)
            except PydanticValidationError as exc:
                await websocket.send_json({"type": "error", "detail": str(exc)})
                continue

            try:
                async with session_factory() as db:
                    response = await execute_prediction(payload, db, orchestration_engine)
                await websocket.send_json({
                    "type": "ack",
                    "mission_id": response.mission_id,
                    "engine_id": response.engine_id,
                    "rule_used": response.rule_used,
                })
            except UnknownEngineError as exc:
                await websocket.send_json({"type": "error", "detail": str(exc)})
            except Exception:
                logger.exception("WS ingest: error processing message from user=%s", claims.sub)
                await websocket.send_json({"type": "error", "detail": "internal error processing reading"})
    except WebSocketDisconnect:
        pass
    finally:
        logger.info("WS ingest disconnected (user=%s)", claims.sub)

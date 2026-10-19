"""
Adapter that exposes the vendored AeroTwin physics engine
(vendor/aerotwin_physics, aerotwin-physics==1.0.0) through the same
`InferenceModel` interface as the ML/toy-physics models, so the orchestrator
can route piston-engine drones to it without special-casing it in
orchestration/router.py.

Two things make this different from the other models in this package, and
both are handled here rather than leaking into the orchestrator:

1. **It's stateful, not a pure function.** `TwinService.ingest()` steps a
   per-engine Kalman drift estimator and AR(1) noise filter -- calling it
   twice for the same sample corrupts that state, and calling it for the
   wrong engine's session mixes two airframes' degradation histories. This
   adapter owns a single `TwinService` and a `(mission_id, engine_id) ->
   session_id` map, lazily opening a session on first telemetry and reusing
   it for every subsequent sample from that engine on that mission.

2. **One physics call answers three orchestration tasks.** A single
   `ingest()` returns anomaly detection, fault diagnosis, and (once enough
   samples have accumulated) RUL together. The routing config
   (config/model_routing.yaml) reflects this: piston-engine drone rules call
   this model exactly once per request, tagged with the
   "combined_assessment" task, instead of three separate steps -- calling it
   three times would triple-step the internal drift/noise state for one
   real-world sample.

Telemetry contract: `sensor_payload` must be keyed by AeroTwin's 13 channel
names (`aerotwin_physics.config.CHANNELS`) -- egt_a, egt_b, oil_pressure,
sensor_4, rpm, map_kpa, fuel_flow, vibration, turbine_inlet_temp_a/b,
sensor_11, cht_a, cht_b -- NOT the generic vibration/temperature/pressure
keys the toy models use. `context.operating_point` must carry the six
required OperatingPoint fields (altitude_m, airspeed_ms, throttle_position,
engine_load, propeller_pitch_deg, fuel_flow_command); ambient fields are
optional and, if supplied from a static port / OAT probe, are preferred over
the engine's ISA fallback.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Make the vendored package importable. It's plain source under vendor/, not
# pip-installed (the upstream distribution's pyproject.toml references an
# `aerotwin_backend` package that isn't present in what was delivered, so
# `pip install .` on it would fail -- see README "Known limitations" note).
# --------------------------------------------------------------------------- #
_VENDOR_DIR = Path(__file__).resolve().parents[2] / "vendor"
if str(_VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_DIR))

from aerotwin_physics import EngineConfig  # noqa: E402
from aerotwin_physics.config import CHANNELS  # noqa: E402
from aerotwin_physics.exceptions import AeroTwinError  # noqa: E402
from aerotwin_physics.service import SessionNotFound, TwinService  # noqa: E402

# CPU-bound, but measured well under 1ms per call in practice (see integration
# notes) -- a thread pool still avoids ever blocking the event loop on it.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="aerotwin-inference")

REQUIRED_OPERATING_POINT_FIELDS = (
    "altitude_m", "airspeed_ms", "throttle_position",
    "engine_load", "propeller_pitch_deg", "fuel_flow_command",
)


class AeroTwinPhysicsModel:
    """Registry-compatible wrapper. Constructor signature matches what
    ModelRegistry.load_all() already calls for every model
    (`version`, `artifact_path`), so no changes to registry.py are needed --
    `artifact_path` is accepted for interface symmetry but unused: this
    model always uses AeroTwin's bundled, version-pinned calibration
    (vendor/aerotwin_physics/data/calibration.json). Point it at an
    alternate calibration file by passing an `EngineConfig` built from that
    file into `TwinService` below if/when recalibration is needed.
    """

    def __init__(self, version: str, artifact_path: str | None = None):
        self.version = version
        self._service = TwinService(EngineConfig.default())
        self._sessions: dict[str, str] = {}  # (mission_id, engine_id) -> session_id
        self._session_lock = threading.Lock()

    def _session_key(self, mission_id: str, engine_id: str) -> str:
        return f"{mission_id}:{engine_id}"

    def _get_or_open_session(self, mission_id: str, engine_id: str) -> str:
        key = self._session_key(mission_id, engine_id)
        with self._session_lock:
            session_id = self._sessions.get(key)
            if session_id is not None:
                # Confirm it hasn't been reaped by TwinService's own TTL.
                try:
                    self._service.get_session(session_id)
                    return session_id
                except SessionNotFound:
                    logger.info("AeroTwin session %s expired, re-opening for %s", session_id, key)
                    self._sessions.pop(key, None)

            session = self._service.open_session(engine_id=engine_id, mission_id=mission_id)
            self._sessions[key] = session.session_id
            return session.session_id

    def close_session(self, mission_id: str, engine_id: str) -> dict | None:
        """Call when a mission ends so the session (and its history) is
        released instead of waiting out the idle TTL. Safe to call even if
        no session was ever opened."""
        key = self._session_key(mission_id, engine_id)
        with self._session_lock:
            session_id = self._sessions.pop(key, None)
        if session_id is None:
            return None
        try:
            return self._service.close_session(session_id)
        except SessionNotFound:
            return None

    async def predict(self, sensor_payload: dict, context) -> tuple[dict, float]:
        if context is None or context.operating_point is None:
            raise AeroTwinError(
                "AeroTwin requires context.operating_point "
                f"(fields: {REQUIRED_OPERATING_POINT_FIELDS})"
            )
        missing_op_fields = [f for f in REQUIRED_OPERATING_POINT_FIELDS
                              if f not in context.operating_point]
        if missing_op_fields:
            raise AeroTwinError(f"operating_point missing fields: {missing_op_fields}")

        missing_channels = [c for c in CHANNELS if c not in sensor_payload]
        if missing_channels:
            raise AeroTwinError(
                f"AeroTwin telemetry missing channels: {missing_channels}. "
                f"Expected all of: {list(CHANNELS)}"
            )

        telemetry = {c: _as_value(sensor_payload[c]) for c in CHANNELS}
        session_id = await asyncio.get_running_loop().run_in_executor(
            _EXECUTOR, self._get_or_open_session, context.mission_id, context.engine_id
        )

        loop = asyncio.get_running_loop()
        body = await loop.run_in_executor(
            _EXECUTOR, self._service.ingest, session_id, context.operating_point, telemetry, None
        )

        confidence = _overall_confidence(body)
        return body, confidence


def _as_value(reading) -> float:
    """sensor_payload entries may be raw floats or {"value": ..., "unit": ...}
    dicts (the shape used elsewhere in this backend); accept both."""
    if isinstance(reading, dict):
        return float(reading["value"])
    return float(reading)


def _overall_confidence(body: dict) -> float:
    """Single scalar confidence for the orchestrator's fallback/threshold
    logic. Anomaly score already lives on [0, 1] and is the most decision-
    relevant number here (routing's on_low_confidence_below is checked
    against this) -- diagnosis.confidence and rul.confidence are carried
    through untouched inside `body` for downstream persistence."""
    return float(body["health"]["anomaly_score"])

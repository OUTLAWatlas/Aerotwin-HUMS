"""Framework-agnostic service layer.

This is the seam between the physics engine and whatever serves it. It knows
nothing about FastAPI, HTTP or WebSockets - it deals in plain dicts and dataclasses
- which means the whole backend behaviour is unit-testable without spinning up a
server, and the same logic can be driven from a REST handler, a WebSocket loop,
an MQTT subscriber or a batch job.

The two things a backend actually needs and the bare engine does not provide:

* **Session lifecycle.** :class:`RealTimeMonitor` is stateful (AR noise, drift
  estimator, sensor validator, residual reference). One monitor per engine per
  flight, created on arm, disposed on land. Sharing a monitor across airframes
  silently mixes their degradation histories.
* **Concurrency safety.** A single monitor must not be stepped by two requests at
  once, or the Kalman recursion interleaves and the drift estimate is corrupted.
  Each session carries its own lock.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import numpy as np

from .config import CHANNELS, FAULT_MODES, EngineConfig
from .diagnostics import GENERALISED_WEAR, NO_FAULT
from .exceptions import AeroTwinError
from .logging_utils import get_logger
from .sensors import OperatingPoint
from .simulator import MonitorOutput, RealTimeMonitor, TrajectorySimulator

logger = get_logger(__name__)

#: Fields a telemetry payload must carry to describe the operating point.
OPERATING_POINT_FIELDS = ("altitude_m", "airspeed_ms", "throttle_position",
                          "engine_load", "propeller_pitch_deg", "fuel_flow_command")


class SessionNotFound(AeroTwinError):
    """Raised when a session id does not exist or has already been closed."""


class SessionLimitExceeded(AeroTwinError):
    """Raised when the configured maximum number of live sessions is reached."""


@dataclass
class SessionStats:
    """Rolling counters for one monitoring session."""
    samples: int = 0
    anomalies: int = 0
    edge_triggers: int = 0
    rejected_samples: int = 0
    last_diagnosis: str = NO_FAULT
    last_rul_s: Optional[float] = None
    last_health: Optional[float] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Session:
    """One engine under monitoring."""
    session_id: str
    engine_id: str
    mission_id: Optional[str]
    monitor: RealTimeMonitor
    stats: SessionStats = field(default_factory=SessionStats)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    closed: bool = False

    def describe(self) -> Dict[str, Any]:
        return {"session_id": self.session_id, "engine_id": self.engine_id,
                "mission_id": self.mission_id, "closed": self.closed,
                **self.stats.as_dict()}


def parse_operating_point(payload: Mapping[str, Any]) -> OperatingPoint:
    """Build an :class:`OperatingPoint` from a request payload.

    Missing optional ambient values are left as ``None`` so the physics core
    falls back to ISA; supplying them (from a static port / OAT probe) is
    preferred and materially more accurate.
    """
    missing = [f for f in OPERATING_POINT_FIELDS if f not in payload]
    if missing:
        raise AeroTwinError(f"operating point missing required fields: {missing}")
    optional = ("wastegate_position", "boost_command", "isa_deviation_c",
                "vertical_speed_ms", "time_s", "ambient_pressure_pa",
                "ambient_temperature_c", "ambient_density_kg_m3")
    kwargs: Dict[str, Any] = {f: float(payload[f]) for f in OPERATING_POINT_FIELDS}
    for name in optional:
        if payload.get(name) is not None:
            kwargs[name] = float(payload[name])
    return OperatingPoint(**kwargs)


def serialise_output(output: MonitorOutput, session: Session) -> Dict[str, Any]:
    """Flatten a :class:`MonitorOutput` into a JSON-safe response body."""
    residual = output.residual
    body: Dict[str, Any] = {
        "session_id": session.session_id,
        "engine_id": session.engine_id,
        "time_s": output.time_s,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "health": {
            "mahalanobis_distance": residual.mahalanobis,
            "anomaly_score": residual.anomaly_score,
            "anomaly_flag": residual.anomaly_flag,
            "edge_trigger": output.edge_trigger,
            "residual_norm": float(np.linalg.norm(residual.residual)),
        },
        "residuals": {c: float(residual.residual[i]) for i, c in enumerate(CHANNELS)},
        "diagnosis": {
            "fault": output.diagnosis.fault,
            "confidence": output.diagnosis.confidence,
            "method": output.diagnosis.method,
            "explanation": output.diagnosis.explain(),
            "similarities": output.diagnosis.similarities,
            "ml_agreement": output.diagnosis.ml_agreement,
        },
        "sensor_validity": {c: bool(v) for c, v in output.valid.items()},
        "rul": None,
    }
    if output.rul is not None:
        rul = output.rul
        body["rul"] = {
            "median_s": rul.median, "mean_s": rul.mean,
            "lower_95_s": rul.lower, "upper_95_s": rul.upper,
            "std_s": rul.std, "confidence": rul.confidence,
            "provisional": rul.provisional,
            "health_index": rul.health_index,
            "degradation": rul.degradation,
            "degradation_rate_per_s": rul.drift,
            "display": rul.render(),
        }
    return body


class TwinService:
    """Stateful application service wrapping the physics engine.

    Thread-safe. Instantiate once per process and share it; the per-session locks
    handle concurrent access to individual engines while allowing different
    engines to be processed in parallel.
    """

    def __init__(self, config: Optional[EngineConfig] = None,
                 max_sessions: int = 64,
                 session_ttl_s: float = 6 * 3600.0) -> None:
        self.config = config or EngineConfig.default()
        self.max_sessions = max_sessions
        self.session_ttl_s = session_ttl_s
        self._sessions: Dict[str, Session] = {}
        self._registry_lock = threading.Lock()
        self._last_touch: Dict[str, float] = {}
        self._started_at = time.time()
        logger.info("TwinService ready (config %s)", self.config.fingerprint())

    # -- lifecycle ---------------------------------------------------------------------
    def open_session(self, engine_id: str, mission_id: Optional[str] = None,
                     session_id: Optional[str] = None) -> Session:
        """Create a monitoring session for one engine on one flight."""
        with self._registry_lock:
            self._reap_expired_locked()
            if len(self._sessions) >= self.max_sessions:
                raise SessionLimitExceeded(
                    f"maximum of {self.max_sessions} concurrent sessions reached")
            sid = session_id or str(uuid.uuid4())
            if sid in self._sessions:
                raise AeroTwinError(f"session {sid} already exists")
            session = Session(session_id=sid, engine_id=engine_id,
                              mission_id=mission_id,
                              monitor=RealTimeMonitor(self.config))
            self._sessions[sid] = session
            self._last_touch[sid] = time.time()
        logger.info("opened session %s for engine %s", sid, engine_id)
        return session

    def get_session(self, session_id: str) -> Session:
        with self._registry_lock:
            session = self._sessions.get(session_id)
        if session is None or session.closed:
            raise SessionNotFound(f"no live session with id {session_id!r}")
        return session

    def close_session(self, session_id: str) -> Dict[str, Any]:
        """Close a session and return its final summary."""
        session = self.get_session(session_id)
        with session.lock:
            session.closed = True
            summary = session.describe()
        with self._registry_lock:
            self._sessions.pop(session_id, None)
            self._last_touch.pop(session_id, None)
        logger.info("closed session %s after %d samples", session_id, summary["samples"])
        return summary

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._registry_lock:
            return [s.describe() for s in self._sessions.values()]

    def _reap_expired_locked(self) -> None:
        """Drop sessions idle beyond the TTL. Caller must hold the registry lock.

        Without this a long-running service leaks a monitor per aborted flight;
        each holds residual history and a drift estimator, and they are never
        collected because the registry still references them.
        """
        now = time.time()
        expired = [sid for sid, touched in self._last_touch.items()
                   if now - touched > self.session_ttl_s]
        for sid in expired:
            logger.warning("reaping idle session %s", sid)
            self._sessions.pop(sid, None)
            self._last_touch.pop(sid, None)

    # -- monitoring --------------------------------------------------------------------
    def ingest(self, session_id: str, operating_point: Mapping[str, Any],
               telemetry: Mapping[str, float],
               time_s: Optional[float] = None) -> Dict[str, Any]:
        """Process one telemetry sample and return the full assessment."""
        session = self.get_session(session_id)
        op = parse_operating_point(operating_point)
        missing = [c for c in CHANNELS if c not in telemetry]
        if missing:
            session.stats.rejected_samples += 1
            raise AeroTwinError(f"telemetry missing channels: {missing}")
        sample = {c: float(telemetry[c]) for c in CHANNELS}
        with session.lock:
            output = session.monitor.step(op, sample, time_s)
            stats = session.stats
            stats.samples += 1
            stats.anomalies += int(output.residual.anomaly_flag)
            stats.edge_triggers += int(output.edge_trigger)
            stats.last_diagnosis = output.diagnosis.fault
            stats.updated_at = datetime.now(timezone.utc).isoformat()
            if output.rul is not None:
                stats.last_rul_s = output.rul.median
                stats.last_health = output.rul.health_index
            body = serialise_output(output, session)
        with self._registry_lock:
            self._last_touch[session_id] = time.time()
        return body

    def ingest_batch(self, session_id: str,
                     samples: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        """Process an ordered burst of samples (e.g. a datalink backlog flush).

        Order matters: the drift estimator and AR noise state are sequential, so
        a store-and-forward buffer must be replayed in acquisition order, oldest
        first, exactly as the edge buffer's priority flush delivers it.
        """
        results = []
        for entry in samples:
            results.append(self.ingest(
                session_id, entry["operating_point"], entry["telemetry"],
                entry.get("time_s")))
        return results

    def session_history(self, session_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        """Recent monitor outputs for a session (newest last)."""
        session = self.get_session(session_id)
        with session.lock:
            history = session.monitor.history[-limit:]
            return [serialise_output(o, session) for o in history]

    # -- offline / reference -----------------------------------------------------------
    def simulate(self, *, index: int = 0, fault_mode: Optional[str] = None,
                 regime: str = "healthy", force_fault: Optional[bool] = None,
                 mission_type: Optional[str] = None) -> Dict[str, Any]:
        """Generate a labelled trajectory. Useful for demos, tests and UI seeding."""
        if fault_mode is not None and fault_mode not in FAULT_MODES:
            raise AeroTwinError(
                f"unknown fault mode {fault_mode!r}; known: {sorted(FAULT_MODES)}")
        simulator = TrajectorySimulator(self.config)
        trajectory = simulator.simulate(
            index=index, fault_mode=fault_mode, regime=regime,
            force_fault=force_fault if force_fault is not None else fault_mode is not None,
            mission_type=mission_type)
        return {
            "trajectory_id": trajectory.trajectory_id,
            "engine_id": trajectory.engine_id,
            "mission_type": trajectory.mission_type,
            "regime": trajectory.regime,
            "fault": {"mode": trajectory.schedule.mode,
                      "onset_s": trajectory.schedule.onset_s,
                      "duration_s": trajectory.schedule.duration_s,
                      "severity_max": trajectory.schedule.severity_max,
                      "ramp_exponent": trajectory.schedule.ramp_exponent},
            "times_s": trajectory.times_s.tolist(),
            "severity": trajectory.severity.tolist(),
            "health_true": trajectory.health.tolist(),
            "telemetry": {c: trajectory.observed[c].tolist() for c in CHANNELS},
            "expected": {c: trajectory.expected[c].tolist() for c in CHANNELS},
            "operating_points": [
                {f: getattr(o, f) for f in OPERATING_POINT_FIELDS}
                for o in trajectory.operating_points],
        }

    # -- introspection -------------------------------------------------------------------
    def metadata(self) -> Dict[str, Any]:
        """Static description of the deployed model - surface this in the UI."""
        from . import __version__
        from .faults import FaultLibrary
        library = FaultLibrary(self.config)
        return {
            "version": __version__,
            "config_fingerprint": self.config.fingerprint(),
            "calibration_source": self.config.metadata.get("calibration_source"),
            "channels": list(CHANNELS),
            "unknown_channels": [c for c in CHANNELS if c.startswith("sensor_")],
            "fault_modes": list(FAULT_MODES) + [GENERALISED_WEAR],
            "fault_signatures": {
                m: dict(library.dominant_channels(m, top=4)) for m in library.names()},
            "anomaly_threshold": self.config.anomaly.threshold,
            "failure_threshold": self.config.degradation.failure_threshold,
            "rul_confidence": self.config.prognostics.confidence,
        }

    def health(self) -> Dict[str, Any]:
        """Liveness/readiness payload for orchestrator probes."""
        with self._registry_lock:
            live = len(self._sessions)
        return {"status": "ok", "uptime_s": round(time.time() - self._started_at, 1),
                "live_sessions": live, "max_sessions": self.max_sessions,
                "config_fingerprint": self.config.fingerprint()}

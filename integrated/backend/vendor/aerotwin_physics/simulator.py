"""Trajectory simulation and the real-time monitoring loop.

Two entry points, for the two ways this package gets used:

* :class:`TrajectorySimulator` - offline. Generates labelled trajectories
  (ground-truth fault, severity, degradation, health) for training, regression
  testing and demo scenarios.
* :class:`RealTimeMonitor` - online. Consumes one telemetry sample at a time and
  emits residual, anomaly, diagnosis and RUL. This is the object a monitoring
  pipeline actually instantiates.

The plant/twin separation described in :class:`~aerotwin_physics.sensors.EnginePhysicsCore`
is enforced here: the simulator holds a faulted plant instance, the monitor holds
a clean twin instance, and they are never the same object.

``RealTimeMonitor`` now sources its health index from Model 4
(:class:`~aerotwin_physics.degradation.MLHealthIndexModel`) and its RUL
estimate from Model 5 (:class:`~aerotwin_physics.prognostics.MLRULPredictor`)
rather than the single-parameter health formula and Wiener/Kalman estimator --
see those modules' docstrings for the validated numbers behind that choice.
``TrajectorySimulator`` is unaffected: forward ground-truth generation still
uses :class:`~aerotwin_physics.degradation.WienerDegradation`, which is a
different job (generating a trajectory) from estimating one online.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import numpy as np

from .config import CHANNELS, EngineConfig
from .degradation import MLHealthIndexModel, WienerDegradation
from .diagnostics import NO_FAULT, DiagnosticEngine, Diagnosis
from .exceptions import AeroTwinError, PrognosticsError
from .faults import FaultInjector, FaultLibrary, FaultSchedule
from .logging_utils import get_logger, log_duration
from .mission import MissionProfile, MissionProfileGenerator
from .noise import SensorNoiseModel, SensorValidator
from .prognostics import MLRULPredictor, RULEstimate
from .residuals import ResidualEngine, ResidualSample
from .sensors import EnginePhysicsCore, OperatingPoint

logger = get_logger(__name__)


@dataclass
class TrajectoryResult:
    """One simulated trajectory with ground truth and derived telemetry."""
    trajectory_id: str
    engine_id: str
    mission_type: str
    schedule: FaultSchedule
    regime: str
    times_s: np.ndarray
    observed: Dict[str, np.ndarray]
    expected: Dict[str, np.ndarray]
    severity: np.ndarray
    degradation: np.ndarray
    health: np.ndarray
    operating_points: List[OperatingPoint]
    phases: List[str]

    @property
    def residuals(self) -> np.ndarray:
        """(n_samples, 13) residual matrix in canonical channel order."""
        return np.column_stack([self.observed[c] - self.expected[c] for c in CHANNELS])

    @property
    def fault_type(self) -> np.ndarray:
        """Per-sample ground-truth label (``none`` before onset)."""
        active = self.severity > 0
        labels = np.full(self.times_s.shape, NO_FAULT, dtype=object)
        if self.schedule.active:
            labels[active] = self.schedule.mode
        return labels

    def to_frame(self):
        """Flatten to a pandas DataFrame in the reference dataset's column style."""
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover
            raise AeroTwinError("pandas is required for to_frame()") from exc
        data: Dict[str, Any] = {
            "scenario_id": self.trajectory_id, "engine_id": self.engine_id,
            "mission_type": self.mission_type, "mission_phase": self.phases,
            "simulation_time": self.times_s, "sample_index": np.arange(len(self.times_s)),
            "fault_type": self.fault_type, "fault_severity": self.severity,
            "fault_onset": self.schedule.onset_s, "fault_duration": self.schedule.duration_s,
            "fault_progress": self.schedule.progress(self.times_s),
            "degradation_true": self.degradation, "health_true": self.health,
        }
        for op_attr in ("altitude_m", "airspeed_ms", "throttle_position", "engine_load",
                        "propeller_pitch_deg", "fuel_flow_command"):
            data[op_attr] = [getattr(o, op_attr) for o in self.operating_points]
        for c in CHANNELS:
            data[c] = self.observed[c]
            data[f"expected_{c}"] = self.expected[c]
            data[f"residual_{c}"] = self.observed[c] - self.expected[c]
        return pd.DataFrame(data)


class TrajectorySimulator:
    """Generates labelled synthetic trajectories.

    Every trajectory is reproducible from ``(config.simulation.random_seed,
    trajectory_index)``: the per-trajectory RNG is spawned from the seed sequence
    rather than shared, so adding a trajectory does not perturb the ones before it.
    """

    def __init__(self, config: Optional[EngineConfig] = None) -> None:
        self.config = config or EngineConfig.default()
        self.plant = EnginePhysicsCore(self.config)
        self.library = FaultLibrary(self.config)
        self.injector = FaultInjector(self.library)
        self.profiles = MissionProfileGenerator(self.config)

    def _rng(self, index: int) -> np.random.Generator:
        seed = self.config.simulation.random_seed
        if seed is None:
            return np.random.default_rng()
        return np.random.default_rng([seed, index])

    def simulate(self, index: int = 0, *, fault_mode: Optional[str] = None,
                 force_fault: Optional[bool] = None, regime: str = "healthy",
                 mission_type: Optional[str] = None,
                 engine_id: Optional[str] = None,
                 trajectory_id: Optional[str] = None) -> TrajectoryResult:
        """Simulate one trajectory end to end."""
        rng = self._rng(index)
        profile = self.profiles.generate(rng, mission_type=mission_type)
        schedule = self.library.sample_schedule(
            profile.duration_s, rng, mode=fault_mode, force=force_fault)
        degradation_model = WienerDegradation(self.config.degradation, regime)
        degradation = degradation_model.simulate(profile.times_s, rng)
        severity = np.asarray(schedule.severity(profile.times_s, rng), float)

        expected = self.plant.nominal_state_batch(profile.operating_points)
        faulted = self.injector.apply(expected, schedule, severity, degradation)

        noise = SensorNoiseModel(self.config.noise, rng)
        observed: Dict[str, List[float]] = {c: [] for c in CHANNELS}
        dt = self.config.simulation.sample_interval_s
        for i in range(len(profile)):
            sample = noise.apply({c: faulted[c][i] for c in CHANNELS}, dt_s=dt)
            for c in CHANNELS:
                observed[c].append(sample[c])

        traj_id = trajectory_id or f"{regime.upper()}-{index:03d}"
        return TrajectoryResult(
            trajectory_id=traj_id,
            engine_id=engine_id or f"ENG-{1000 + index % 12:04d}",
            mission_type=profile.mission_type, schedule=schedule, regime=regime,
            times_s=profile.times_s,
            observed={c: np.asarray(v, float) for c, v in observed.items()},
            expected={c: np.asarray(expected[c], float) for c in CHANNELS},
            severity=severity, degradation=degradation,
            health=1.0 - degradation,
            operating_points=profile.operating_points, phases=profile.phases)

    def simulate_batch(self, n: int, *, fault_fraction: float = 0.5,
                       regimes: Optional[Sequence[str]] = None) -> List[TrajectoryResult]:
        """Generate a labelled batch spanning healthy, degrading and faulted engines."""
        regimes = list(regimes or ["healthy", "degradation", "fault", "rul"])
        modes = self.library.names()
        results: List[TrajectoryResult] = []
        for i in range(n):
            rng = self._rng(10_000 + i)
            faulted = rng.random() < fault_fraction
            regime = regimes[i % len(regimes)] if faulted else "healthy"
            results.append(self.simulate(
                index=i, force_fault=faulted,
                fault_mode=str(rng.choice(modes)) if faulted else None,
                regime=regime))
        logger.info("simulated %d trajectories (%d faulted)", n,
                    sum(r.schedule.active for r in results))
        return results


@dataclass
class MonitorOutput:
    """One step of the online monitoring loop.

    ``health_index`` is stored directly here (not only inside ``rul``)
    because :class:`MLHealthIndexModel` produces one every step, while
    ``rul`` is ``None`` for the first ``min_samples_for_estimate`` samples --
    callers that only want the health trend should not have to wait for RUL
    to become available too.
    """
    time_s: float
    residual: ResidualSample
    diagnosis: Diagnosis
    health_index: float
    rul: Optional[RULEstimate]
    edge_trigger: bool
    valid: Dict[str, bool]

    def as_dict(self) -> Dict[str, Any]:
        out = {"time_s": self.time_s, "edge_trigger": self.edge_trigger,
               "health_index": self.health_index}
        out.update(self.residual.as_dict())
        out.update(self.diagnosis.as_dict())
        if self.rul is not None:
            out.update(self.rul.as_dict())
        return out


class RealTimeMonitor:
    """Online health monitor: one telemetry sample in, full assessment out.

    Deployment shape
    ----------------
    The heavy math (Mahalanobis inversion, ExtraTrees inference) belongs at
    the ground station. The ``edge_trigger`` output is the cheap part that can
    run on a SWaP-constrained onboard computer: it is a threshold on the
    normalised residual norm and needs no matrix inverse or trained artefact,
    so it can raise an immediate sub-threshold deviation alert without
    waiting on a datalink round trip.

    Health (Model 4) and RUL (Model 5) are now served by trained artefacts
    (:class:`~aerotwin_physics.degradation.MLHealthIndexModel`,
    :class:`~aerotwin_physics.prognostics.MLRULPredictor`) rather than the
    closed-form physics estimators this class used previously. Both are
    stateful per engine -- health needs a 10-sample rolling window, RUL needs
    a running sample count -- which is exactly why they are owned here, one
    instance per session, and reset alongside everything else in
    :meth:`reset`.
    """

    def __init__(self, config: Optional[EngineConfig] = None,
                 edge_trigger_quantile: float = 0.60) -> None:
        self.config = config or EngineConfig.default()
        self.twin = EnginePhysicsCore(self.config)        # clean baseline, no faults
        self.residuals = ResidualEngine(self.config)
        self.diagnostics = DiagnosticEngine(self.config)
        self.health_model = MLHealthIndexModel()          # Model 4
        self.rul_model = MLRULPredictor()                 # Model 5
        self.validator = SensorValidator()
        self._edge_threshold = self.residuals.threshold_distance(edge_trigger_quantile)
        self._history: List[MonitorOutput] = []

    def reset(self) -> None:
        """Clear per-flight state. Call between missions."""
        self.health_model.reset()
        self.rul_model.reset()
        self.validator.reset()
        self._history.clear()

    @property
    def history(self) -> List[MonitorOutput]:
        return list(self._history)

    def step(self, operating_point: OperatingPoint,
             observed: Mapping[str, float],
             time_s: Optional[float] = None) -> MonitorOutput:
        """Process one telemetry sample."""
        t = float(time_s if time_s is not None else operating_point.time_s)
        with log_duration(logger, "monitor.step"):
            missing = [c for c in CHANNELS if c not in observed]
            if missing:
                raise AeroTwinError(f"telemetry sample missing channels: {missing}")
            valid = self.validator.validate({c: float(observed[c]) for c in CHANNELS})
            expected = self.twin.nominal_state(operating_point)
            residual = self.residuals.evaluate(observed, expected, t, valid)
            diagnosis = self.diagnostics.diagnose(
                residual.residual, residual.anomaly_flag, residual.valid_mask)

            health_index = self.health_model.update_and_predict(
                residual.residual, mahalanobis=residual.mahalanobis)

            # nominal_state() resolves internally but doesn't hand the
            # resolved object back -- resolve it again here (cheap, and
            # idempotent) so requested_power/mixture_setting/ambient_* are
            # actual floats, not the None placeholders OperatingPoint starts
            # with. Feeding those Nones into the feature row is what caused
            # MLRULPredictor to blow up on float(None).
            resolved_op = self.twin.resolve(operating_point)

            feature_row: Dict[str, Any] = {
                "time_s": t,
                "health_index": health_index,
                **{c: float(observed[c]) for c in CHANNELS},
                **{f"expected_{c}": float(expected[c]) for c in CHANNELS},
                **vars(resolved_op),
            }
            self.rul_model.update(feature_row)
            try:
                rul = self.rul_model.estimate(feature_row)
            except PrognosticsError:
                rul = None   # too early in the flight to say anything honest

            edge = bool(residual.mahalanobis > self._edge_threshold)
            self.residuals.update_reference(residual.residual)
        output = MonitorOutput(time_s=t, residual=residual, diagnosis=diagnosis,
                               health_index=health_index, rul=rul,
                               edge_trigger=edge, valid=valid)
        self._history.append(output)
        return output

    def run(self, operating_points: Sequence[OperatingPoint],
            observations: Sequence[Mapping[str, float]]) -> List[MonitorOutput]:
        """Replay a whole trajectory through the monitor."""
        if len(operating_points) != len(observations):
            raise AeroTwinError("operating_points and observations must be equal length")
        return [self.step(op, obs) for op, obs in zip(operating_points, observations)]

    def replay(self, trajectory: TrajectoryResult) -> List[MonitorOutput]:
        """Convenience: replay a simulated trajectory through the monitor."""
        self.reset()
        observations = [{c: trajectory.observed[c][i] for c in CHANNELS}
                        for i in range(len(trajectory.times_s))]
        return self.run(trajectory.operating_points, observations)
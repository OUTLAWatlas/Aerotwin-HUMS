"""Configuration schema and loader.

Every tunable in the physics engine lives in a dataclass here. Nothing in the
simulation path reads a magic number from module scope: the whole configuration
is serialisable to YAML/JSON, hashable for provenance, and validated on
construction. That is what makes a run reproducible six months later when
someone asks why a particular RUL number was reported.

The default values are *calibrated against the reference dataset*
(``phm_synthetic_dataset_v2-2.xlsx``), not invented. See
``aerotwin_physics/data/calibration.json`` and the README for the derivation.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .exceptions import ConfigurationError
from .logging_utils import get_logger

logger = get_logger(__name__)

# --------------------------------------------------------------------------------------
# Channel registry
# --------------------------------------------------------------------------------------

#: The 13 core sensor channels, in the canonical order used for every residual
#: vector, covariance matrix and fault-signature vector in this package.
#:
#: ``sensor_4`` and ``sensor_11`` are deliberately *not* given physical names.
#: In the reference dataset their correlation evidence was confounded
#: (``sensor_4`` correlates with airspeed and altitude, but those correlate with
#: throttle themselves), so the dataset authors left them ``UNKNOWN`` rather than
#: fabricate a mapping. We preserve that: they are modelled numerically and
#: carried through the pipeline, but no physical interpretation is asserted and
#: the diagnostics layer down-weights them (see ``DiagnosticsConfig``).
CHANNELS: Tuple[str, ...] = (
    "egt_a", "egt_b", "oil_pressure", "sensor_4", "rpm", "map_kpa", "fuel_flow",
    "vibration", "turbine_inlet_temp_a", "turbine_inlet_temp_b", "sensor_11",
    "cht_a", "cht_b",
)

#: Channels whose physical meaning is unconfirmed in the source data.
UNKNOWN_CHANNELS: Tuple[str, ...] = ("sensor_4", "sensor_11")

#: The five fault modes present in the reference taxonomy.
FAULT_MODES: Tuple[str, ...] = (
    "misfire", "ring_wear", "cooling_restriction", "oil_starvation", "wastegate_leak",
)

#: Physical units, where known. ``None`` marks a non-calibrated / arbitrary-unit
#: channel. The reference dataset is explicitly *not* unit-calibrated for several
#: channels (EGT range 489-755 in "arbitrary units"), so downstream code must
#: never assume degrees Celsius or Kelvin for these.
CHANNEL_UNITS: Dict[str, Optional[str]] = {
    "egt_a": None, "egt_b": None, "oil_pressure": "psi-plausible",
    "sensor_4": None, "rpm": "rev/min", "map_kpa": "kPa", "fuel_flow": None,
    "vibration": None, "turbine_inlet_temp_a": None, "turbine_inlet_temp_b": None,
    "sensor_11": None, "cht_a": None, "cht_b": None,
}


# --------------------------------------------------------------------------------------
# Sub-configurations
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AtmosphereConfig:
    """ISA atmosphere parameters.

    Validated against the reference dataset: the recovered relation
    ``T = 15 - 0.0065*h + dT_isa`` matches to 1e-5 K and
    ``rho = p / (R * T)`` matches to 5e-6 kg/m^3.
    """
    sea_level_pressure_pa: float = 101_325.0
    sea_level_temperature_c: float = 15.0
    lapse_rate_k_per_m: float = 0.0065
    gas_constant_j_per_kg_k: float = 287.05
    pressure_exponent: float = 5.25588
    pressure_lapse_coeff: float = 2.25577e-5


@dataclass(frozen=True)
class OperatingBounds:
    """Validated operating envelope.

    Bounds are the min/max observed across the 22 reference trajectories, widened
    by ``margin_frac``. Outside them the calibrated sensor surfaces are
    extrapolating and their accuracy is unwarranted.
    """
    altitude_m: Tuple[float, float] = (0.0, 4_438.0)
    airspeed_ms: Tuple[float, float] = (68.4, 209.7)
    throttle: Tuple[float, float] = (0.226, 0.919)
    engine_load: Tuple[float, float] = (0.184, 0.831)
    propeller_pitch_deg: Tuple[float, float] = (20.37, 38.87)
    fuel_flow_command: Tuple[float, float] = (8.33, 25.92)
    wastegate_position: Tuple[float, float] = (0.305, 0.888)
    boost_command: Tuple[float, float] = (0.294, 0.908)
    margin_frac: float = 0.10

    def widened(self, name: str) -> Tuple[float, float]:
        """Return a bound widened by ``margin_frac`` on each side."""
        lo, hi = getattr(self, name)
        span = hi - lo
        return lo - self.margin_frac * span, hi + self.margin_frac * span


@dataclass(frozen=True)
class NoiseConfig:
    """Sensor noise model.

    Observed noise on the reference healthy trajectories is approximately
    *multiplicative* (0.8-1.9% of channel magnitude) with mild positive lag-1
    autocorrelation on ``egt_a`` (0.58) and ``map_kpa`` (0.49) - i.e. coloured,
    not white. Modelling it as AR(1) matters: a white-noise assumption makes the
    Mahalanobis detector over-confident and inflates the false-alarm rate.
    """
    relative_sigma: Dict[str, float] = field(default_factory=dict)
    ar1_coefficient: Dict[str, float] = field(default_factory=dict)
    reference_scale: Dict[str, float] = field(default_factory=dict)
    enable: bool = True
    #: Probability per sample that a channel reports a dropout (held last value).
    dropout_probability: float = 0.0
    #: Slow additive bias accumulated per second, as a fraction of channel scale.
    drift_rate_per_s: float = 0.0


@dataclass(frozen=True)
class FaultModeConfig:
    """Parameters of one fault mode.

    ``channel_gains`` are *relative* deviations at unit severity: the observed
    value becomes ``expected * (1 + gain * severity)``. They were recovered by
    least-squares from the reference fault trajectories, jointly with the
    degradation term so that the slow-wear contribution is not double-counted.
    """
    name: str
    channel_gains: Dict[str, float]
    #: Exponent of the severity ramp ``severity = severity_max * progress**k``.
    #: The reference trajectories span k in [1.00, 2.95]; sampling within this
    #: range is what makes synthetic onsets look like real ones rather than
    #: uniformly linear.
    ramp_exponent_range: Tuple[float, float] = (1.0, 2.95)
    severity_max_range: Tuple[float, float] = (0.75, 0.98)
    #: Fraction of the mission at which onset may occur.
    onset_fraction_range: Tuple[float, float] = (0.20, 0.80)
    #: Per-sample multiplicative jitter on severity, as seen in the source data
    #: (severity is not monotone sample-to-sample).
    severity_jitter: float = 0.25


@dataclass(frozen=True)
class FaultConfig:
    """Fault-injection configuration for a simulation run."""
    modes: Dict[str, FaultModeConfig] = field(default_factory=dict)
    #: Probability that a randomly-generated trajectory contains a fault at all.
    fault_probability: float = 0.5
    #: Degradation sensitivity: relative channel deviation per unit degradation.
    degradation_gains: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class DegradationConfig:
    """Wiener degradation process parameters.

    ``dX = mu*dt + sigma*dW``, ``X`` in [0, 1], health ``H = 1 - X``.
    The four regimes reproduce the reference dataset's trajectory families.
    """
    regimes: Dict[str, Dict[str, float]] = field(default_factory=lambda: {
        "healthy":     {"drift": 0.0,       "volatility": 7.2e-7, "x0": 0.0041},
        "degradation": {"drift": 7.08e-5,   "volatility": 9.3e-6, "x0": 0.0005},
        "fault":       {"drift": 5.3e-8,    "volatility": 7.6e-7, "x0": 0.0349},
        "rul":         {"drift": 2.507e-4,  "volatility": 2.5e-7, "x0": 0.0002},
    })
    #: Degradation level treated as end-of-life for first-hitting-time RUL.
    failure_threshold: float = 0.75
    default_regime: str = "healthy"


@dataclass(frozen=True)
class AnomalyConfig:
    """Residual anomaly detector settings.

    The score is the chi-square CDF of the squared Mahalanobis distance, which
    reproduces the reference dataset's ``anomaly_score`` to a mean absolute error
    of 0.018. The trip threshold of 0.70 is the dataset's own
    ``anomaly_threshold`` column and is kept as the default for comparability.
    """
    threshold: float = 0.70
    #: Empirical scale factor relating a healthy-covariance Mahalanobis distance
    #: to the dataset's reported distance (the source applied shrinkage).
    mahalanobis_scale: float = 1.0
    #: Ridge added to the covariance diagonal before inversion. Non-negotiable in
    #: production: a 13x13 covariance estimated from a short calibration flight is
    #: frequently near-singular, and an un-regularised inverse turns sensor noise
    #: into spurious 40-sigma excursions.
    covariance_ridge: float = 1e-9
    #: EWMA rate for adaptive covariance tracking (0 disables adaptation).
    ewma_alpha: float = 0.0
    min_calibration_samples: int = 30


@dataclass(frozen=True)
class DiagnosticsConfig:
    """Fault isolation settings."""
    #: Softmax temperature over cosine similarities. Lower = more decisive.
    softmax_temperature: float = 0.15
    #: Similarity below which no fault is declared (returns ``none``).
    min_similarity: float = 0.30
    #: Weight applied to the physically-unconfirmed channels when forming the
    #: similarity. Down-weighted rather than dropped: they carry signal (sensor_4
    #: responds strongly to cooling_restriction and oil_starvation) but should not
    #: drive a diagnosis whose physical story cannot be told to an operator.
    unknown_channel_weight: float = 0.5
    #: Enable the supervised cross-check model when training data is available.
    enable_ml_crosscheck: bool = True


@dataclass(frozen=True)
class PrognosticsConfig:
    """Inverse-Gaussian / first-hitting-time RUL settings."""
    confidence: float = 0.95
    #: Minimum samples before an RUL estimate is anything but provisional.
    min_samples_for_estimate: int = 10
    #: Samples below which the estimate is flagged ``provisional=True``.
    provisional_sample_count: int = 40
    #: Kalman process/measurement noise for the recursive drift update.
    drift_process_noise: float = 1e-10
    drift_measurement_noise: float = 1e-6
    #: Health-index mapping ``H = exp(-rho * D_mahalanobis)``.
    health_index_rho: float = 0.02
    #: Clamp on reported RUL, in seconds. Guards against the near-zero-drift case
    #: producing an astronomically large "safe" number.
    max_rul_s: float = 1e6


@dataclass(frozen=True)
class SimulationConfig:
    """Trajectory generation settings."""
    sample_interval_s: float = 60.0
    samples_per_trajectory: int = 50
    random_seed: Optional[int] = 42
    mission_types: Sequence[str] = ("cargo", "patrol", "training", "test_flight")
    mission_phases: Sequence[str] = ("climb", "cruise", "loiter", "descent")


@dataclass(frozen=True)
class EngineConfig:
    """Top-level configuration object.

    Construct via :meth:`load` (from YAML/JSON) or :meth:`default` (bundled
    dataset calibration). Instances are frozen; use :meth:`replace` for variants.
    """
    atmosphere: AtmosphereConfig = field(default_factory=AtmosphereConfig)
    bounds: OperatingBounds = field(default_factory=OperatingBounds)
    noise: NoiseConfig = field(default_factory=NoiseConfig)
    faults: FaultConfig = field(default_factory=FaultConfig)
    degradation: DegradationConfig = field(default_factory=DegradationConfig)
    anomaly: AnomalyConfig = field(default_factory=AnomalyConfig)
    diagnostics: DiagnosticsConfig = field(default_factory=DiagnosticsConfig)
    prognostics: PrognosticsConfig = field(default_factory=PrognosticsConfig)
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    #: ``"raise"`` rejects out-of-envelope operating points; ``"clip"`` clamps
    #: them and logs a warning; ``"allow"`` disables the check entirely.
    bounds_policy: str = "clip"
    #: Free-form provenance recorded into every simulation output.
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.bounds_policy not in ("raise", "clip", "allow"):
            raise ConfigurationError(
                f"bounds_policy must be raise|clip|allow, got {self.bounds_policy!r}")
        if not 0.0 < self.anomaly.threshold < 1.0:
            raise ConfigurationError("anomaly.threshold must lie in (0, 1)")
        if not 0.0 < self.prognostics.confidence < 1.0:
            raise ConfigurationError("prognostics.confidence must lie in (0, 1)")
        if self.degradation.failure_threshold <= 0:
            raise ConfigurationError("degradation.failure_threshold must be positive")
        unknown = set(self.faults.modes) - set(FAULT_MODES)
        if unknown:
            raise ConfigurationError(f"unknown fault modes configured: {sorted(unknown)}")

    # -- construction ------------------------------------------------------------------
    @classmethod
    def default(cls, calibration_path: Optional[os.PathLike] = None) -> "EngineConfig":
        """Build a configuration from the bundled dataset calibration."""
        from .calibration import load_calibration, config_from_calibration
        return config_from_calibration(load_calibration(calibration_path))

    @classmethod
    def load(cls, path: os.PathLike) -> "EngineConfig":
        """Load configuration from a ``.yaml``/``.yml``/``.json`` file.

        Unknown top-level keys raise rather than being ignored: a typo'd
        ``anomally_threshold`` that silently keeps the default is exactly the
        class of bug that surfaces as an unexplained field failure.
        """
        path = Path(path)
        if not path.exists():
            raise ConfigurationError(f"configuration file not found: {path}")
        text = path.read_text(encoding="utf-8")
        try:
            if path.suffix.lower() in (".yaml", ".yml"):
                import yaml  # optional dependency
                payload = yaml.safe_load(text)
            else:
                payload = json.loads(text)
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ConfigurationError(
                "PyYAML is required to read YAML configs; use JSON or `pip install pyyaml`"
            ) from exc
        except Exception as exc:
            raise ConfigurationError(f"could not parse {path}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise ConfigurationError(f"{path} must contain a mapping at the top level")
        logger.info("loaded configuration from %s", path)
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EngineConfig":
        """Build from a nested mapping, starting from the calibrated defaults."""
        base = cls.default()
        return _merge_dataclass(base, payload)

    # -- serialisation -----------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable nested dict of the whole configuration."""
        return asdict(self)

    def save(self, path: os.PathLike) -> None:
        """Persist the configuration next to the run artefacts it produced."""
        path = Path(path)
        payload = self.to_dict()
        if path.suffix.lower() in (".yaml", ".yml"):
            import yaml
            path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        logger.info("saved configuration to %s", path)

    def replace(self, **changes: Any) -> "EngineConfig":
        """Return a copy with top-level fields replaced (validated)."""
        import dataclasses
        return dataclasses.replace(self, **changes)

    def fingerprint(self) -> str:
        """Stable short hash of the configuration, for run provenance."""
        import hashlib
        blob = json.dumps(self.to_dict(), sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


# --------------------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------------------

def _merge_dataclass(instance: Any, payload: Mapping[str, Any]) -> Any:
    """Recursively overlay ``payload`` onto a dataclass instance, validating keys."""
    import dataclasses
    valid = {f.name: f for f in fields(instance)}
    unknown = set(payload) - set(valid)
    if unknown:
        raise ConfigurationError(
            f"unknown configuration keys for {type(instance).__name__}: {sorted(unknown)}")
    changes: Dict[str, Any] = {}
    for key, value in payload.items():
        current = getattr(instance, key)
        if is_dataclass(current) and isinstance(value, Mapping):
            changes[key] = _merge_dataclass(current, value)
        elif isinstance(current, tuple) and isinstance(value, (list, tuple)):
            changes[key] = tuple(value)
        else:
            changes[key] = value
    return dataclasses.replace(instance, **changes)

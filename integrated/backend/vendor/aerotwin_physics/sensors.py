"""Deterministic sensor response surfaces.

Every one of the 13 channels is expressed as an explicit algebraic function of a
small set of core operating variables. Eleven of the thirteen are *exact* -
recovered from the reference dataset to a maximum relative error of 0.002%, with
round-number coefficients that are almost certainly the generator's own
constants. The two turbine-inlet-temperature channels have no exact closed form
in the source and are represented by a calibrated quadratic response surface
(R^2 = 0.999991, residual sigma 0.22 in channel units).

Core state variables
--------------------
Six independent commands/conditions drive everything:

===========================  ==========================================================
``altitude_m``               Geometric altitude; also sets ambient p, T, rho and mixture
``airspeed_ms``              True airspeed; drives convective cooling of the heads
``throttle_position``        [0, 1]; primary EGT / RPM / MAP driver
``engine_load``              [0, 1]; ``requested_power = 300 * engine_load``
``propeller_pitch_deg``      Blade angle; enters the TIT surface
``fuel_flow_command``        Commanded fuel flow; sole driver of the ``fuel_flow`` channel
===========================  ==========================================================

Two further quantities are *derived*, not free, and the engine computes them
rather than accepting them, because in the reference data they are exact
functions of the above:

* ``requested_power = 300 * engine_load``
* ``mixture_setting = 1 - 4e-5 * altitude_m``  (altitude mixture leaning schedule)

``wastegate_position`` and ``boost_command`` are carried as recorded commands for
provenance but do not enter any exact sensor map; the wastegate's physical effect
appears through the ``wastegate_leak`` fault channel gains instead.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, Mapping, Optional

import numpy as np

from .atmosphere import ISAAtmosphere
from .calibration import load_calibration
from .config import CHANNELS, EngineConfig
from .exceptions import CalibrationError, OperatingBoundsError
from .logging_utils import get_logger

logger = get_logger(__name__)

#: Fixed scaling between normalised engine load and the power demand signal.
POWER_PER_UNIT_LOAD = 300.0
#: Altitude mixture-leaning schedule slope, per metre.
MIXTURE_LEAN_PER_M = 4.0e-5


@dataclass(frozen=True)
class OperatingPoint:
    """One evaluated operating condition.

    Instances are immutable and cheap to construct; a trajectory is just a
    sequence of them. Ambient conditions are filled in by
    :meth:`EnginePhysicsCore.resolve` if left at ``None``.
    """
    altitude_m: float
    airspeed_ms: float
    throttle_position: float
    engine_load: float
    propeller_pitch_deg: float
    fuel_flow_command: float
    wastegate_position: float = 0.62
    boost_command: float = 0.63
    isa_deviation_c: float = 0.0
    vertical_speed_ms: float = 0.0
    time_s: float = 0.0
    # Derived (populated by resolve()):
    requested_power: Optional[float] = None
    mixture_setting: Optional[float] = None
    ambient_temperature_c: Optional[float] = None
    ambient_pressure_pa: Optional[float] = None
    ambient_density_kg_m3: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class SensorResponseModel:
    """Closed-form nominal sensor maps.

    This class is deliberately stateless and vectorised: every method accepts
    either scalars or NumPy arrays, so the same code path serves a 1 Hz real-time
    monitoring loop and a batch replay of a whole mission archive.
    """

    def __init__(self, calibration: Optional[Mapping[str, Any]] = None) -> None:
        cal = calibration or load_calibration()
        try:
            surface = cal["tit_surface"]
            self._tit_features = list(surface["features"])
            self._tit_models = surface["models"]
        except (KeyError, TypeError) as exc:
            raise CalibrationError(f"malformed tit_surface calibration: {exc}") from exc

    # -- exact channels -----------------------------------------------------------------
    @staticmethod
    def egt_a(throttle: Any, altitude_m: Any) -> Any:
        """Exhaust gas temperature, bank A. Exact: ``420 + 300*thr + 0.003*h``."""
        return 420.0 + 300.0 * np.asarray(throttle) + 0.003 * np.asarray(altitude_m)

    @staticmethod
    def egt_b(throttle: Any, altitude_m: Any) -> Any:
        """Exhaust gas temperature, bank B. Slightly lower gain than bank A -
        the asymmetry is what makes a single-bank misfire separable."""
        return 410.0 + 290.0 * np.asarray(throttle) + 0.003 * np.asarray(altitude_m)

    @staticmethod
    def oil_pressure(requested_power: Any, altitude_m: Any) -> Any:
        """Lubrication pressure. Rises with power, falls with altitude."""
        return 60.0 + np.asarray(requested_power) / 15.0 - 0.0009 * np.asarray(altitude_m)

    @staticmethod
    def sensor_4(requested_power: Any, altitude_m: Any) -> Any:
        """Unconfirmed channel 4 (candidate: oil temperature - NOT established).

        Modelled numerically because it carries real diagnostic signal, but no
        physical interpretation is asserted: the source evidence was confounded
        by the mutual correlation of airspeed, altitude and throttle.
        """
        return 180.0 + 0.004 * np.asarray(altitude_m) + 0.15 * np.asarray(requested_power)

    @staticmethod
    def rpm(throttle: Any) -> Any:
        """Crankshaft speed. Pure function of throttle in the reference envelope."""
        return 2000.0 + 550.0 * np.asarray(throttle)

    @staticmethod
    def map_kpa(ambient_pressure_pa: Any, throttle: Any) -> Any:
        """Manifold absolute pressure.

        ``MAP = p_ambient * (0.30 + 0.72 * throttle)``: the 0.30 term is the
        closed-throttle floor, the 0.72 term the throttle-plate opening. Because
        MAP scales with *ambient* pressure, a wastegate leak and a climb look
        similar on this channel alone - which is precisely why the twin compares
        against an altitude-aware expectation instead of a fixed threshold.
        """
        return (np.asarray(ambient_pressure_pa) / 1000.0) * (
            0.30 + 0.72 * np.asarray(throttle))

    @staticmethod
    def fuel_flow(fuel_flow_command: Any) -> Any:
        """Metered fuel flow. Exact: ``5 + (55/3) * command``."""
        return 5.0 + (55.0 / 3.0) * np.asarray(fuel_flow_command)

    @staticmethod
    def vibration(engine_load: Any) -> Any:
        """Crankcase vibration amplitude. Quadratic in load: ``0.4 + 1.6 * load^2``.

        The quadratic term matters for prognostics: vibration is the single most
        responsive channel to both misfire (gain 0.69) and ring wear (0.63), and
        its curvature means a late-stage fault moves it far more than an early one.
        """
        return 0.4 + 1.6 * np.asarray(engine_load) ** 2

    @staticmethod
    def sensor_11(requested_power: Any) -> Any:
        """Unconfirmed channel 11. Linear in power; no physical claim made."""
        return 150.0 + (17.0 / 60.0) * np.asarray(requested_power)

    @staticmethod
    def cht_a(requested_power: Any, airspeed_ms: Any) -> Any:
        """Cylinder head temperature, bank A.

        The negative airspeed coefficient is the convective cooling term; it is
        what the ``cooling_restriction`` fault attacks (gain +0.32, the largest
        single-channel fault gain in the taxonomy).
        """
        return 300.0 + 0.5 * np.asarray(requested_power) - 0.01 * np.asarray(airspeed_ms)

    @staticmethod
    def cht_b(requested_power: Any, airspeed_ms: Any) -> Any:
        """Cylinder head temperature, bank B."""
        return 295.0 + (29.0 / 60.0) * np.asarray(requested_power) \
            - 0.009 * np.asarray(airspeed_ms)

    # -- calibrated surface -------------------------------------------------------------
    def _tit(self, channel: str, features: Mapping[str, Any]) -> Any:
        model = self._tit_models[channel]
        first = next(iter(features.values()))
        total = np.full(np.shape(np.asarray(first)), float(model["intercept"]))
        for term, coeff in model["terms"].items():
            value = np.ones_like(total)
            for part in term.split(" "):
                if part.endswith("^2"):
                    value = value * np.asarray(features[part[:-2]], float) ** 2
                else:
                    value = value * np.asarray(features[part], float)
            total = total + coeff * value
        return total

    def turbine_inlet_temp(self, channel: str, **features: Any) -> Any:
        """Turbine inlet temperature from the calibrated quadratic surface.

        Unlike the other channels this is an empirical surrogate, not a recovered
        exact form. It is accurate (R^2 = 0.999991) but it is a *regression*, and
        it is the one map that must not be trusted outside the calibrated
        envelope - hence the bounds check in :class:`EnginePhysicsCore`.
        """
        if channel not in self._tit_models:
            raise CalibrationError(f"no TIT surface calibrated for {channel!r}")
        return self._tit(channel, features)

    # -- full evaluation ----------------------------------------------------------------
    def evaluate(self, op: OperatingPoint) -> Dict[str, Any]:
        """Return the nominal (healthy, noise-free) value of all 13 channels."""
        power = op.requested_power
        alt, spd, thr = op.altitude_m, op.airspeed_ms, op.throttle_position
        tit_features = {
            "throttle_position": thr,
            "propeller_pitch": op.propeller_pitch_deg,
            "mixture_setting": op.mixture_setting,
            "wastegate_position": op.wastegate_position,
            "boost_command": op.boost_command,
            "altitude": alt,
            "airspeed": spd,
            "engine_load": op.engine_load,
            "fuel_flow_command": op.fuel_flow_command,
        }
        return {
            "egt_a": self.egt_a(thr, alt),
            "egt_b": self.egt_b(thr, alt),
            "oil_pressure": self.oil_pressure(power, alt),
            "sensor_4": self.sensor_4(power, alt),
            "rpm": self.rpm(thr),
            "map_kpa": self.map_kpa(op.ambient_pressure_pa, thr),
            "fuel_flow": self.fuel_flow(op.fuel_flow_command),
            "vibration": self.vibration(op.engine_load),
            "turbine_inlet_temp_a": self.turbine_inlet_temp(
                "turbine_inlet_temp_a", **tit_features),
            "turbine_inlet_temp_b": self.turbine_inlet_temp(
                "turbine_inlet_temp_b", **tit_features),
            "sensor_11": self.sensor_11(power),
            "cht_a": self.cht_a(power, spd),
            "cht_b": self.cht_b(power, spd),
        }


class EnginePhysicsCore:
    """The nominal-state physics reference.

    Instantiate this **twice** in a full digital-twin deployment:

    1. as the *plant*, with fault injection enabled, to generate ground truth or
       to drive a hardware-in-the-loop rig;
    2. as the *twin baseline*, with faults disabled, to produce the expectation
       the live telemetry is differenced against.

    Sharing one instance between the two roles is the classic way to make a twin
    look perfect in testing and useless in flight: the residual becomes
    identically zero by construction and the detector never trips.
    """

    def __init__(self, config: Optional[EngineConfig] = None,
                 calibration: Optional[Mapping[str, Any]] = None) -> None:
        self.config = config or EngineConfig.default()
        self.atmosphere = ISAAtmosphere(self.config.atmosphere)
        self.sensors = SensorResponseModel(calibration)
        self._bounds_warned = False

    # -- envelope handling ---------------------------------------------------------------
    def _check_bounds(self, op: OperatingPoint) -> OperatingPoint:
        policy = self.config.bounds_policy
        if policy == "allow":
            return op
        checks = {
            "altitude_m": ("altitude_m", op.altitude_m),
            "airspeed_ms": ("airspeed_ms", op.airspeed_ms),
            "throttle": ("throttle_position", op.throttle_position),
            "engine_load": ("engine_load", op.engine_load),
            "propeller_pitch_deg": ("propeller_pitch_deg", op.propeller_pitch_deg),
            "fuel_flow_command": ("fuel_flow_command", op.fuel_flow_command),
            "wastegate_position": ("wastegate_position", op.wastegate_position),
            "boost_command": ("boost_command", op.boost_command),
        }
        changes: Dict[str, float] = {}
        for bound_name, (attr, value) in checks.items():
            lo, hi = self.config.bounds.widened(bound_name)
            if value < lo or value > hi:
                if policy == "raise":
                    raise OperatingBoundsError(
                        f"{attr}={value:.4g} outside validated envelope [{lo:.4g}, {hi:.4g}]")
                changes[attr] = float(np.clip(value, lo, hi))
        if changes:
            if not self._bounds_warned:
                logger.warning("clipping out-of-envelope operating point: %s "
                               "(further occurrences logged at DEBUG)", changes)
                self._bounds_warned = True
            else:
                logger.debug("clipping out-of-envelope operating point: %s", changes)
            op = replace(op, **changes)
        return op

    # -- public API ----------------------------------------------------------------------
    def resolve(self, op: OperatingPoint) -> OperatingPoint:
        """Fill in derived quantities (power, mixture, ambient state) and clip.

        Ambient pressure and temperature supplied on the ``OperatingPoint`` are
        *preferred over* the ISA model. This is deliberate and matters for
        accuracy: a real airframe has a static port and an OAT probe, and the
        measured value differs from the ISA prediction by weather. In the
        reference dataset that difference is up to 130 Pa, which propagates
        directly into MAP (MAP scales with ambient pressure) and would otherwise
        appear as a spurious 0.18% residual on a perfectly healthy engine.

        ISA is the fallback for planning, replay and what-if scenarios where no
        measurement exists - not the primary source when one does.
        """
        op = self._check_bounds(op)
        needs_atmosphere = (op.ambient_pressure_pa is None
                            or op.ambient_temperature_c is None
                            or op.ambient_density_kg_m3 is None)
        if needs_atmosphere:
            atm = self.atmosphere.state(op.altitude_m, op.isa_deviation_c)
            pressure = op.ambient_pressure_pa if op.ambient_pressure_pa is not None \
                else float(atm.pressure_pa)
            temperature = op.ambient_temperature_c if op.ambient_temperature_c is not None \
                else float(atm.temperature_c)
            density = op.ambient_density_kg_m3 if op.ambient_density_kg_m3 is not None \
                else float(self.atmosphere.density_kg_m3(pressure, temperature))
        else:
            pressure = op.ambient_pressure_pa
            temperature = op.ambient_temperature_c
            density = op.ambient_density_kg_m3
        return replace(
            op,
            requested_power=POWER_PER_UNIT_LOAD * op.engine_load,
            mixture_setting=1.0 - MIXTURE_LEAN_PER_M * op.altitude_m,
            ambient_temperature_c=temperature,
            ambient_pressure_pa=pressure,
            ambient_density_kg_m3=density,
        )

    def nominal_state(self, op: OperatingPoint) -> Dict[str, float]:
        """Nominal, healthy, noise-free channel values at an operating point.

        This is ``f_thermo(x, u)`` in the hybrid-twin formulation
        ``y_hat = f_thermo(x, u) + g_theta(x, u)``.
        """
        return {k: float(v) for k, v in
                self.sensors.evaluate(self.resolve(op)).items()}

    def nominal_state_batch(self, ops) -> Dict[str, np.ndarray]:
        """Vectorised nominal state for a sequence of operating points."""
        resolved = [self.resolve(o) for o in ops]
        stacked = {
            "altitude_m": np.array([o.altitude_m for o in resolved]),
            "airspeed_ms": np.array([o.airspeed_ms for o in resolved]),
            "throttle_position": np.array([o.throttle_position for o in resolved]),
            "engine_load": np.array([o.engine_load for o in resolved]),
            "propeller_pitch_deg": np.array([o.propeller_pitch_deg for o in resolved]),
            "fuel_flow_command": np.array([o.fuel_flow_command for o in resolved]),
            "wastegate_position": np.array([o.wastegate_position for o in resolved]),
            "boost_command": np.array([o.boost_command for o in resolved]),
            "requested_power": np.array([o.requested_power for o in resolved]),
            "mixture_setting": np.array([o.mixture_setting for o in resolved]),
            "ambient_pressure_pa": np.array([o.ambient_pressure_pa for o in resolved]),
            "ambient_temperature_c": np.array([o.ambient_temperature_c for o in resolved]),
        }
        proto = OperatingPoint(
            altitude_m=stacked["altitude_m"], airspeed_ms=stacked["airspeed_ms"],
            throttle_position=stacked["throttle_position"],
            engine_load=stacked["engine_load"],
            propeller_pitch_deg=stacked["propeller_pitch_deg"],
            fuel_flow_command=stacked["fuel_flow_command"],
            wastegate_position=stacked["wastegate_position"],
            boost_command=stacked["boost_command"],
            requested_power=stacked["requested_power"],
            mixture_setting=stacked["mixture_setting"],
            ambient_pressure_pa=stacked["ambient_pressure_pa"],
            ambient_temperature_c=stacked["ambient_temperature_c"],
        )
        return {k: np.asarray(v, float) for k, v in self.sensors.evaluate(proto).items()}

    def channel_vector(self, state: Mapping[str, float]) -> np.ndarray:
        """Order a channel mapping into the canonical residual-vector layout."""
        return np.array([float(state[c]) for c in CHANNELS])

"""Mission profile generation.

Produces the command/condition trajectories that drive the physics core. Profiles
are phase-structured (climb, cruise, loiter, descent) rather than random walks,
because the correlation structure between altitude, throttle and load is exactly
what the twin has to learn to see through - a fault detector trained on
uncorrelated random inputs will treat an ordinary climb as an anomaly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

import numpy as np

from .config import EngineConfig, SimulationConfig
from .sensors import OperatingPoint

#: Phase templates: (altitude fraction of ceiling, throttle, load, vertical speed m/s)
_PHASE_TEMPLATES: Dict[str, Dict[str, float]] = {
    "climb":   {"alt_frac": 0.55, "throttle": 0.78, "load": 0.70, "vs": 5.0},
    "cruise":  {"alt_frac": 0.95, "throttle": 0.62, "load": 0.55, "vs": 0.0},
    "loiter":  {"alt_frac": 0.85, "throttle": 0.42, "load": 0.35, "vs": 0.0},
    "descent": {"alt_frac": 0.40, "throttle": 0.32, "load": 0.28, "vs": -4.0},
}

#: Phase sequence by mission type, as fractions of total mission duration.
_MISSION_PROFILES: Dict[str, Sequence[tuple]] = {
    "cargo":       (("climb", 0.25), ("cruise", 0.55), ("descent", 0.20)),
    "patrol":      (("climb", 0.15), ("cruise", 0.20), ("loiter", 0.50), ("descent", 0.15)),
    "training":    (("climb", 0.20), ("cruise", 0.30), ("loiter", 0.30), ("descent", 0.20)),
    "test_flight": (("climb", 0.30), ("cruise", 0.25), ("loiter", 0.25), ("descent", 0.20)),
}


@dataclass(frozen=True)
class MissionProfile:
    """A generated mission: time grid, phase labels and operating points."""
    mission_type: str
    duration_s: float
    times_s: np.ndarray
    phases: List[str]
    operating_points: List[OperatingPoint]

    def __len__(self) -> int:
        return len(self.operating_points)

    @property
    def progress(self) -> np.ndarray:
        return self.times_s / max(self.duration_s, 1e-9)


class MissionProfileGenerator:
    """Builds phase-structured mission profiles within the validated envelope."""

    def __init__(self, config: Optional[EngineConfig] = None) -> None:
        self.config = config or EngineConfig.default()
        self.settings: SimulationConfig = self.config.simulation

    def _phase_sequence(self, mission_type: str, n: int) -> List[str]:
        plan = _MISSION_PROFILES.get(mission_type, _MISSION_PROFILES["cargo"])
        labels: List[str] = []
        for phase, frac in plan:
            labels.extend([phase] * max(1, int(round(frac * n))))
        if len(labels) < n:
            labels.extend([plan[-1][0]] * (n - len(labels)))
        return labels[:n]

    def generate(self, rng: np.random.Generator,
                 mission_type: Optional[str] = None,
                 n_samples: Optional[int] = None,
                 ceiling_m: float = 4_400.0) -> MissionProfile:
        """Generate one mission profile.

        Small per-sample jitter is added to every command. Without it the commands
        are piecewise constant, the residual becomes trivially clean within a
        phase, and the detector's measured false-alarm rate is meaninglessly good.
        """
        n = n_samples or self.settings.samples_per_trajectory
        dt = self.settings.sample_interval_s
        mission_type = mission_type or str(rng.choice(list(self.settings.mission_types)))
        times = np.arange(n, dtype=float) * dt
        duration = float(times[-1] + dt)
        phases = self._phase_sequence(mission_type, n)
        bounds = self.config.bounds

        points: List[OperatingPoint] = []
        altitude = 0.0
        for i, phase in enumerate(phases):
            template = _PHASE_TEMPLATES[phase]
            target_alt = template["alt_frac"] * ceiling_m
            # First-order approach to the phase's target altitude.
            altitude += 0.35 * (target_alt - altitude) + rng.normal(0.0, 60.0)
            altitude = float(np.clip(altitude, *bounds.altitude_m))
            throttle = float(np.clip(template["throttle"] + rng.normal(0.0, 0.035),
                                     *bounds.throttle))
            load = float(np.clip(template["load"] + rng.normal(0.0, 0.03),
                                 *bounds.engine_load))
            airspeed = float(np.clip(
                110.0 + 90.0 * throttle + 0.004 * altitude + rng.normal(0.0, 4.0),
                *bounds.airspeed_ms))
            pitch = float(np.clip(24.0 + 14.0 * load + rng.normal(0.0, 0.8),
                                  *bounds.propeller_pitch_deg))
            ffc = float(np.clip(6.0 + 20.0 * load + rng.normal(0.0, 0.4),
                                *bounds.fuel_flow_command))
            wastegate = float(np.clip(0.35 + 0.42 * throttle + rng.normal(0.0, 0.02),
                                      *bounds.wastegate_position))
            boost = float(np.clip(0.30 + 0.48 * throttle + rng.normal(0.0, 0.02),
                                  *bounds.boost_command))
            points.append(OperatingPoint(
                altitude_m=altitude, airspeed_ms=airspeed, throttle_position=throttle,
                engine_load=load, propeller_pitch_deg=pitch, fuel_flow_command=ffc,
                wastegate_position=wastegate, boost_command=boost,
                isa_deviation_c=float(rng.normal(0.0, 0.85)),
                vertical_speed_ms=float(template["vs"] + rng.normal(0.0, 1.0)),
                time_s=float(times[i])))
        return MissionProfile(mission_type=mission_type, duration_s=duration,
                              times_s=times, phases=phases, operating_points=points)

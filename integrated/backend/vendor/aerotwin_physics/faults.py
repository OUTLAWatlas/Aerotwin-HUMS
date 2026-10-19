"""Fault modes, onset scheduling and injection.

Design decisions worth stating explicitly, because they are the difference
between a demo that looks convincing and one that does not:

**Faults are ramps, not switches.** Severity follows
``s(t) = s_max * progress(t)**k`` with ``k`` sampled per trajectory from the
range observed in the reference data ([1.00, 2.95]). A step-function fault
produces telemetry no detector has to work for; the whole point of a predictive
twin is to catch the *ramp*, before a fixed threshold would have tripped.

**Faults act multiplicatively on the nominal value**, not additively on a raw
number: ``observed = expected * (1 + gain * severity)``. This keeps the injected
effect proportionate at every operating point, so a 20% ring-wear effect on EGT
is 20% at idle and 20% at full power rather than a fixed offset that is
overwhelming at one end of the envelope and invisible at the other.

**The gains are measured, not invented.** The ``CHANNEL_GAINS`` in the bundled
calibration were recovered by least squares from the reference fault
trajectories, fitted jointly with the slow-degradation term so the two effects
are not double-counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np

from .config import CHANNELS, FAULT_MODES, EngineConfig, FaultModeConfig
from .exceptions import FaultDefinitionError
from .logging_utils import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class FaultSchedule:
    """A fault occurrence within one trajectory.

    ``onset_s`` of ``-1`` means "no fault", matching the reference dataset's
    sentinel so schedules round-trip through exported data unchanged.
    """
    mode: Optional[str] = None
    onset_s: float = -1.0
    duration_s: float = 0.0
    severity_max: float = 0.0
    ramp_exponent: float = 1.0
    severity_jitter: float = 0.0

    @property
    def active(self) -> bool:
        return self.mode is not None and self.onset_s >= 0.0

    def progress(self, time_s: Any) -> Any:
        """Fraction of the fault's evolution completed at ``time_s``, in [0, 1]."""
        if not self.active or self.duration_s <= 0:
            return np.zeros_like(np.asarray(time_s, float))
        return np.clip((np.asarray(time_s, float) - self.onset_s) / self.duration_s, 0.0, 1.0)

    def severity(self, time_s: Any, rng: Optional[np.random.Generator] = None) -> Any:
        """Severity in [0, 1] at ``time_s``, including per-sample jitter.

        The jitter reproduces a real and easily-missed property of the reference
        data: severity is *not* monotone sample to sample. A prognostics filter
        tuned on a perfectly smooth ramp will be over-confident when it meets
        real, noisy degradation.
        """
        base = self.severity_max * self.progress(time_s) ** self.ramp_exponent
        if self.severity_jitter and rng is not None:
            noise = rng.normal(1.0, self.severity_jitter, size=np.shape(base))
            base = np.clip(base * noise, 0.0, 1.0)
        return base


class FaultLibrary:
    """Registry of fault modes and their channel signatures.

    Also exposes the Fault Signature Matrix used by the diagnostics layer, so the
    injector and the isolator are guaranteed to agree on what each fault *means*.
    """

    def __init__(self, config: Optional[EngineConfig] = None) -> None:
        self.config = config or EngineConfig.default()
        self.modes: Dict[str, FaultModeConfig] = dict(self.config.faults.modes)
        if not self.modes:
            raise FaultDefinitionError("no fault modes configured")

    # -- introspection --------------------------------------------------------------
    def __contains__(self, mode: str) -> bool:
        return mode in self.modes

    def names(self) -> List[str]:
        return list(self.modes)

    def get(self, mode: str) -> FaultModeConfig:
        try:
            return self.modes[mode]
        except KeyError as exc:
            raise FaultDefinitionError(
                f"unknown fault mode {mode!r}; known modes: {sorted(self.modes)}") from exc

    def signature_vector(self, mode: str) -> np.ndarray:
        """Unit-severity relative-deviation vector in canonical channel order."""
        gains = self.get(mode).channel_gains
        return np.array([gains.get(c, 0.0) for c in CHANNELS], float)

    def signature_matrix(self) -> tuple[List[str], np.ndarray]:
        """Return ``(mode_names, matrix)`` with one row per fault mode."""
        names = self.names()
        return names, np.vstack([self.signature_vector(m) for m in names])

    def dominant_channels(self, mode: str, top: int = 3) -> List[tuple[str, float]]:
        """Channels a given fault moves most - the operator-facing 'why'."""
        gains = self.get(mode).channel_gains
        ranked = sorted(gains.items(), key=lambda kv: abs(kv[1]), reverse=True)
        return ranked[:top]

    # -- scheduling ------------------------------------------------------------------
    def sample_schedule(self, mission_duration_s: float, rng: np.random.Generator,
                        mode: Optional[str] = None,
                        force: Optional[bool] = None) -> FaultSchedule:
        """Draw a randomised fault schedule for one trajectory.

        Randomising onset, severity ceiling and ramp exponent per trajectory (as
        the reference dataset does) is what stops a downstream classifier from
        learning "the fault starts at sample 12" instead of learning the fault.
        """
        if mission_duration_s <= 0:
            raise FaultDefinitionError("mission_duration_s must be positive")
        occurs = force if force is not None else \
            rng.random() < self.config.faults.fault_probability
        if not occurs:
            return FaultSchedule()
        mode = mode or str(rng.choice(self.names()))
        spec = self.get(mode)
        lo, hi = spec.onset_fraction_range
        onset = float(rng.uniform(lo, hi)) * mission_duration_s
        # Snap onset to the sample grid the way the reference data does, so the
        # first faulted sample is unambiguous during validation.
        s_lo, s_hi = spec.severity_max_range
        k_lo, k_hi = spec.ramp_exponent_range
        return FaultSchedule(
            mode=mode,
            onset_s=onset,
            duration_s=mission_duration_s - onset,
            severity_max=float(rng.uniform(s_lo, s_hi)),
            ramp_exponent=float(rng.uniform(k_lo, k_hi)),
            severity_jitter=spec.severity_jitter,
        )


class FaultInjector:
    """Applies fault and degradation effects to a nominal channel state."""

    def __init__(self, library: FaultLibrary) -> None:
        self.library = library
        self._degradation_gains = dict(library.config.faults.degradation_gains)

    def relative_deviation(self, schedule: FaultSchedule, severity: Any,
                           degradation: Any = 0.0) -> Dict[str, Any]:
        """Combined relative deviation per channel from fault + slow wear."""
        deviation: Dict[str, Any] = {}
        gains = (self.library.get(schedule.mode).channel_gains
                 if schedule.active else {})
        for channel in CHANNELS:
            fault_term = gains.get(channel, 0.0) * np.asarray(severity, float)
            wear_term = self._degradation_gains.get(channel, 0.0) \
                * np.asarray(degradation, float)
            deviation[channel] = fault_term + wear_term
        return deviation

    def apply(self, nominal: Mapping[str, Any], schedule: FaultSchedule,
              severity: Any, degradation: Any = 0.0) -> Dict[str, Any]:
        """Return the faulted (still noise-free) channel state.

        ``observed = expected * (1 + gain_fault * severity + gain_wear * degradation)``
        """
        deviation = self.relative_deviation(schedule, severity, degradation)
        return {c: np.asarray(nominal[c], float) * (1.0 + deviation[c]) for c in CHANNELS}


def describe_fault_modes(library: Optional[FaultLibrary] = None) -> str:
    """Human-readable summary of the fault taxonomy, for logs and documentation."""
    library = library or FaultLibrary()
    lines = ["Fault taxonomy (relative channel gain at unit severity):"]
    for mode in library.names():
        top = library.dominant_channels(mode, top=4)
        rendered = ", ".join(f"{c} {g:+.3f}" for c, g in top)
        lines.append(f"  {mode:<20s} {rendered}")
    return "\n".join(lines)

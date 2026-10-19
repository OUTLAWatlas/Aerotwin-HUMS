"""Sensor noise, dropout and drift.

The reference healthy trajectories show noise that is:

* **multiplicative** - standard deviation is 0.8-1.9% of channel magnitude, not a
  fixed absolute value;
* **coloured on two channels** - ``egt_a`` has lag-1 autocorrelation 0.58 and
  ``map_kpa`` 0.49, the rest are effectively white.

Both properties are modelled. Getting the second one wrong is not cosmetic: an
AR(1) residual violates the independence assumption behind the chi-square trip
threshold, and a detector calibrated on white noise will false-alarm on those two
channels at several times the nominal rate.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

import numpy as np

from .config import CHANNELS, NoiseConfig
from .logging_utils import get_logger

logger = get_logger(__name__)


class SensorNoiseModel:
    """Stateful AR(1) + multiplicative Gaussian noise generator.

    State (the previous noise sample per channel) is held internally, so the
    generator must be reset between independent trajectories - see :meth:`reset`.
    """

    def __init__(self, config: NoiseConfig,
                 rng: Optional[np.random.Generator] = None) -> None:
        self.config = config
        self.rng = rng or np.random.default_rng()
        self._previous: Dict[str, float] = {c: 0.0 for c in CHANNELS}
        self._held: Dict[str, Optional[float]] = {c: None for c in CHANNELS}
        self._elapsed_s: float = 0.0

    def reset(self, rng: Optional[np.random.Generator] = None) -> None:
        """Clear AR state between trajectories."""
        self._previous = {c: 0.0 for c in CHANNELS}
        self._held = {c: None for c in CHANNELS}
        self._elapsed_s = 0.0
        if rng is not None:
            self.rng = rng

    def _sigma(self, channel: str, value: float) -> float:
        rel = self.config.relative_sigma.get(channel)
        if rel is None:
            return 0.0
        scale = self.config.reference_scale.get(channel, abs(value))
        # Scale with the *instantaneous* magnitude where that is meaningful, but
        # fall back to the calibrated reference scale near zero so a channel
        # passing through zero does not become noise-free.
        return rel * max(abs(value), 0.25 * abs(scale))

    def apply(self, clean: Mapping[str, Any], dt_s: float = 0.0) -> Dict[str, float]:
        """Corrupt one clean sample. Returns a new dict; input is untouched."""
        if not self.config.enable:
            return {c: float(clean[c]) for c in CHANNELS}
        self._elapsed_s += dt_s
        out: Dict[str, float] = {}
        for channel in CHANNELS:
            value = float(clean[channel])
            sigma = self._sigma(channel, value)
            phi = float(self.config.ar1_coefficient.get(channel, 0.0))
            # Innovation variance chosen so the stationary variance equals sigma^2.
            innovation = self.rng.normal(0.0, sigma * np.sqrt(max(1.0 - phi ** 2, 1e-12)))
            noise = phi * self._previous[channel] + innovation
            self._previous[channel] = noise
            drifted = value + noise
            if self.config.drift_rate_per_s:
                scale = self.config.reference_scale.get(channel, abs(value))
                drifted += self.config.drift_rate_per_s * self._elapsed_s * scale
            if self.config.dropout_probability and \
                    self.rng.random() < self.config.dropout_probability:
                # Dropout: the bus reports the last good value rather than NaN,
                # which is the realistic CAN/FADEC failure mode and the one that
                # silently freezes a residual instead of obviously breaking it.
                held = self._held[channel]
                out[channel] = float(held) if held is not None else drifted
                continue
            self._held[channel] = drifted
            out[channel] = float(drifted)
        return out


class SensorValidator:
    """Cheap plausibility gate applied before the residual is formed.

    Catches instrumentation faults that would otherwise be misattributed to the
    engine. The rate check exploits the fact that thermal channels have known
    time constants: an EGT reading that jumps 30% between consecutive samples is
    physically impossible for a thermal mass and is therefore a sensor fault, not
    a combustion event.
    """

    def __init__(self, max_relative_step: float = 0.30,
                 exempt: tuple = ("vibration",)) -> None:
        self.max_relative_step = max_relative_step
        self.exempt = set(exempt)
        self._last: Dict[str, float] = {}

    def reset(self) -> None:
        self._last.clear()

    def validate(self, sample: Mapping[str, float]) -> Dict[str, bool]:
        """Return a per-channel validity mask.

        Invalid channels should be *masked out* of the Mahalanobis distance, not
        allowed to dominate it. Continuing to monitor on the surviving channels
        beats declaring a mechanical failure because a thermocouple snapped.
        """
        valid: Dict[str, bool] = {}
        for channel, value in sample.items():
            if channel in self.exempt or channel not in self._last:
                valid[channel] = bool(np.isfinite(value))
            else:
                previous = self._last[channel]
                denom = max(abs(previous), 1e-9)
                step = abs(value - previous) / denom
                valid[channel] = bool(np.isfinite(value) and step <= self.max_relative_step)
            if np.isfinite(value):
                self._last[channel] = float(value)
        return valid

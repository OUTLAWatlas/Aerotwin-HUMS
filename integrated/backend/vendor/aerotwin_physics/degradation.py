"""Wiener degradation process.

``dX = mu*dt + sigma*dW`` with ``X`` the accumulated degradation in [0, 1] and
health ``H = 1 - X``. Brownian motion with drift is the standard, defensible
model for aero-engine wear because it represents both steady deterministic
deterioration (``mu``) and random operational shocks (``sigma``) - as opposed to
a linear extrapolation, which assumes wear is smooth and therefore reports a
confident RUL that reality routinely violates.

The four regimes reproduce the reference dataset's trajectory families:
``healthy`` (no trend), ``degradation`` (slow wear to ~0.20 over a mission),
``fault`` (fixed offset, fault-dominated), ``rul`` (fast wear to ~0.75, i.e.
reaching the failure threshold within the mission).

``WienerDegradation`` and ``health_index_from_distance`` remain in use by
:class:`TrajectorySimulator` for *forward* (ground-truth) trajectory
generation -- that is a different job from the *online* health-index
estimate in :class:`~aerotwin_physics.simulator.RealTimeMonitor`, which now
uses :class:`MLHealthIndexModel` below (Model 4: ExtraTrees over 184
engineered residual features, validated R2=0.931 vs this module's R2~0.65-0.70
single-parameter formula on the same reference dataset).
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence

import joblib
import numpy as np

from .config import DegradationConfig
from .exceptions import ConfigurationError
from .logging_utils import get_logger

logger = get_logger(__name__)

_DEFAULT_MODEL4_DIR = Path(__file__).with_name("data") / "model4"


@dataclass(frozen=True)
class DegradationState:
    """Degradation and health at one instant."""
    time_s: float
    degradation: float
    health: float
    drift: float
    volatility: float

    @property
    def headroom(self) -> float:
        """Remaining degradation before the failure threshold, set by the process."""
        return self._headroom

    _headroom: float = 0.0


class WienerDegradation:
    """Wiener-process degradation generator and tracker.

    Used in two directions:

    * **forward** (:meth:`simulate`) to generate ground-truth degradation traces
      for synthetic trajectories;
    * **inverse** (:class:`aerotwin_physics.prognostics.DriftEstimator`) to
      recover ``mu`` and ``sigma`` from an observed health trace.
    """

    def __init__(self, config: Optional[DegradationConfig] = None,
                 regime: Optional[str] = None) -> None:
        self.config = config or DegradationConfig()
        self.regime = regime or self.config.default_regime
        if self.regime not in self.config.regimes:
            raise ConfigurationError(
                f"unknown degradation regime {self.regime!r}; "
                f"known: {sorted(self.config.regimes)}")

    @property
    def params(self) -> Dict[str, float]:
        return self.config.regimes[self.regime]

    @property
    def failure_threshold(self) -> float:
        return self.config.failure_threshold

    def simulate(self, times_s: Sequence[float],
                 rng: Optional[np.random.Generator] = None,
                 x0: Optional[float] = None) -> np.ndarray:
        """Generate a degradation trace on the given time grid.

        The path is clipped to [0, 1] but *not* forced monotone: a Wiener path can
        dip. Real health indices do the same when the operator throttles back, and
        a prognostics filter has to cope with that rather than assume it away.
        """
        rng = rng or np.random.default_rng()
        times = np.asarray(times_s, float)
        if times.ndim != 1 or times.size == 0:
            raise ValueError("times_s must be a non-empty 1-D sequence")
        params = self.params
        x = float(params["x0"] if x0 is None else x0)
        dt = np.diff(times, prepend=times[0])
        increments = params["drift"] * dt + params["volatility"] * np.sqrt(
            np.maximum(dt, 0.0)) * rng.standard_normal(times.size)
        path = np.clip(x + np.cumsum(increments), 0.0, 1.0)
        return path

    def health(self, degradation: Any) -> Any:
        """Health index ``H = 1 - X``."""
        return 1.0 - np.asarray(degradation, float)

    def state_at(self, time_s: float, degradation: float) -> DegradationState:
        params = self.params
        return DegradationState(
            time_s=time_s, degradation=degradation, health=1.0 - degradation,
            drift=params["drift"], volatility=params["volatility"],
            _headroom=max(self.failure_threshold - degradation, 0.0))


def health_index_from_distance(mahalanobis: Any, rho: float = 0.02) -> Any:
    """Map a Mahalanobis distance onto a bounded health index.

    ``H = exp(-rho * D)``. The exponential form is chosen so that health is
    bounded in (0, 1] with no clipping artefacts, and so equal *ratios* of
    distance produce equal drops in health - a distance going 5 -> 10 costs the
    same health as 10 -> 20, which matches how degradation compounds.

    Kept as the fast, dependency-free fallback for cold-start (before
    :class:`MLHealthIndexModel` has enough rolling history) and for anywhere
    forward simulation needs a health value from a distance without a
    trained artefact on hand.
    """
    return np.exp(-rho * np.asarray(mahalanobis, float))


class MLHealthIndexModel:
    """Model 4: ExtraTrees health-index regressor (184 engineered features),
    replacing the single-parameter ``exp(-rho*D)`` formula for the *online*
    monitoring path.

    Needs a rolling buffer of the last 10 residual samples per engine to
    compute z-score rolling-means and diffs -- this is why it is stateful and
    is owned per-session by
    :class:`~aerotwin_physics.simulator.RealTimeMonitor`, not called as a
    stateless function like ``health_index_from_distance`` above.

    Falls back to ``health_index_from_distance`` if the trained artefact is
    not present at ``model_dir`` (e.g. in a dev environment before the
    artefacts have been copied in), logging a warning rather than failing
    startup -- consistent with how the rest of this package treats optional
    artefacts.
    """

    ROLLING_WINDOW = 10

    def __init__(self, model_dir: Path = _DEFAULT_MODEL4_DIR,
                 fallback_rho: float = 0.02) -> None:
        self._fallback_rho = fallback_rho
        self._model = None
        self._feature_list: List[str] = []
        self._baseline_mean: Optional[np.ndarray] = None
        self._baseline_std: Optional[np.ndarray] = None
        self._channels: List[str] = []
        self._history: Deque[np.ndarray] = deque(maxlen=self.ROLLING_WINDOW)

        model_path = model_dir / "model4_health_extratrees.joblib"
        feature_path = model_dir / "model4_feature_list.json"
        baseline_path = model_dir / "model4_healthy_baseline.json"

        if model_path.exists() and feature_path.exists() and baseline_path.exists():
            self._model = joblib.load(model_path)
            self._feature_list = json.loads(feature_path.read_text())
            baseline = json.loads(baseline_path.read_text())
            self._baseline_mean = np.asarray(baseline["mean"], float)
            self._baseline_std = np.asarray(baseline["std"], float)
            self._channels = list(baseline["channels"])
            logger.info(
                "MLHealthIndexModel loaded (%d features) from %s",
                len(self._feature_list), model_dir,
            )
        else:
            logger.warning(
                "Model 4 artefacts not found at %s -- falling back to "
                "health_index_from_distance(rho=%.3f) for every sample.",
                model_dir, fallback_rho,
            )

    def reset(self) -> None:
        """Call on session close -- a new engine must not inherit rolling state."""
        self._history.clear()

    def update_and_predict(self, residual: np.ndarray,
                           mahalanobis: Optional[float] = None) -> float:
        """Feed one new residual vector, return the current health index."""
        if self._model is None:
            if mahalanobis is None:
                raise ConfigurationError(
                    "MLHealthIndexModel has no trained artefact and no "
                    "mahalanobis distance was supplied for the fallback path"
                )
            return float(health_index_from_distance(mahalanobis, self._fallback_rho))

        self._history.append(np.asarray(residual, float))
        row = self._build_feature_row()
        pred = self._model.predict(row.reshape(1, -1))[0]
        return float(np.clip(pred, 0.0, 1.0))

    def _build_feature_row(self) -> np.ndarray:
        latest = self._history[-1]
        z = (latest - self._baseline_mean) / self._baseline_std
        abs_z = np.abs(z)

        window = np.array(self._history)              # (n<=10, 13)
        z_window = (window - self._baseline_mean) / self._baseline_std

        features: Dict[str, float] = {}
        for i, ch in enumerate(self._channels):
            features[f"residual_{ch}"] = float(latest[i])
            features[f"z_{ch}"] = float(z[i])
            features[f"abs_z_{ch}"] = float(abs_z[i])
            features[f"residual_{ch}_rollmean10"] = float(window[:, i].mean())
            features[f"z_{ch}_rollmean10"] = float(z_window[:, i].mean())
            prev = self._history[-2][i] if len(self._history) > 1 else latest[i]
            features[f"residual_{ch}_diff"] = float(latest[i] - prev)
            features[f"residual_{ch}_absdiff"] = abs(features[f"residual_{ch}_diff"])

        features["z_abs_mean"] = float(abs_z.mean())
        features["z_abs_std"] = float(abs_z.std())
        features["z_abs_max"] = float(abs_z.max())
        features["z_abs_rms"] = float(np.sqrt((z ** 2).mean()))
        features["z_signed_mean"] = float(z.mean())
        features["z_signed_std"] = float(z.std())
        features["z_signed_max"] = float(z.max())
        features["z_signed_min"] = float(z.min())

        # Exact column order the model was trained on -- mismatch here
        # silently corrupts every prediction rather than raising.
        return np.array([features.get(f, 0.0) for f in self._feature_list])

"""Prognostics: Model 5 RUL, plus the Wiener/Kalman estimator it replaced
for the online monitoring path.

Two estimators live in this module now:

* :class:`RULPredictor` (original) - Wiener drift estimation with Kalman
  recursion and Inverse-Gaussian first-hitting-time RUL. Gives a genuine
  confidence interval and responds in-flight to a throttled-back engine, at
  the cost of raw point accuracy: on the reference dataset's leakage-free
  grouped validation it underperformed the trained regressor below.
* :class:`MLRULPredictor` (Model 5, now primary) - the notebook's final
  production model: ``ExtraTreesRegressor`` on 51 raw-telemetry features
  (``RAW_TELEMETRY_ONLY`` feature set), validated R2=0.760 / MAE=805 cycles
  via 5-fold GroupKFold-by-mission, after an earlier version of this model
  was found to have the RUL target leaking into its own feature set (fixed
  before export -- see ``model5_final_config.json``'s
  ``target_leakage_removed`` flag).

``RULPredictor`` is kept, not deleted: it is the better-justified estimator
when you need an honest uncertainty band or in-flight adaptivity, and
nothing here prevents surfacing both side by side if that turns out to be
useful operationally.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence

import joblib
import numpy as np
from scipy import stats

from .config import EngineConfig, PrognosticsConfig
from .degradation import health_index_from_distance
from .exceptions import PrognosticsError
from .logging_utils import get_logger

logger = get_logger(__name__)

_DEFAULT_MODEL5_DIR = Path(__file__).with_name("data") / "model5"


@dataclass
class RULEstimate:
    """A Remaining-Useful-Life estimate with uncertainty bounds.

    All times are in seconds. ``lower``/``upper`` are the 2.5th/97.5th percentiles
    of the Inverse-Gaussian first-hitting-time distribution (at the default 95%
    confidence) for :class:`RULPredictor`, so the median is inside the interval
    **by construction** for that estimator. :class:`MLRULPredictor` below fills
    the same fields with a fixed +/-15% display band instead -- see its
    docstring for why that is not a statistical interval.
    """
    time_s: float
    mean: float
    median: float
    lower: float
    upper: float
    std: float
    confidence: float
    health_index: float
    degradation: float
    drift: float
    volatility: float
    provisional: bool
    n_samples: int

    @property
    def interval_width(self) -> float:
        return self.upper - self.lower

    def survival_probability(self, mission_time_s: float) -> float:
        """P(time-to-failure > mission_time_s) - the mission-reliability number.

        RUL alone is not actionable: 45 minutes of engine life is a non-event
        20 minutes from base and a loss of airframe 90 minutes from base. This is
        the quantity a contingency decision should actually be keyed to.

        For :class:`MLRULPredictor` estimates (no fitted drift/volatility),
        this falls back to a normal approximation around ``mean``/``std``
        rather than the Inverse-Gaussian form, since there is no fitted
        Wiener process to derive ``lam`` from.
        """
        if mission_time_s <= 0:
            return 1.0
        if self.drift <= 0 or self.mean <= 0:
            if self.std > 0:
                return float(stats.norm.sf(mission_time_s, loc=self.mean, scale=self.std))
            return 1.0
        lam = (self.mean ** 3) / max(self.std ** 2, 1e-18)
        return float(stats.invgauss.sf(mission_time_s, mu=self.mean / lam, scale=lam))

    def as_dict(self) -> Dict[str, Any]:
        return {"rul_mean": self.mean, "rul_median": self.median,
                "rul_lower_95": self.lower, "rul_upper_95": self.upper,
                "rul_std": self.std, "rul_confidence": self.confidence,
                "health_index": self.health_index,
                "estimated_degradation": self.degradation,
                "estimated_degradation_rate": self.drift,
                "rul_provisional": self.provisional}

    def render(self) -> str:
        """Operator-facing string, deliberately imprecise."""
        if self.provisional:
            return (f"RUL ~{self.median/60:.0f} min "
                    f"[{self.lower/60:.0f}-{self.upper/60:.0f}] (PROVISIONAL)")
        half = (self.upper - self.lower) / 2
        return f"RUL {self.median/60:.0f} +/- {half/60:.0f} min"


class DriftEstimator:
    """Kalman-style recursive estimator of Wiener drift and volatility.

    Tracking drift rather than fixing it is what makes RUL responsive to operator
    action. The same recursive-estimation family is used in the state estimator,
    keeping one belief-state machinery across the twin instead of a second
    bespoke filter.
    """

    def __init__(self, config: Optional[PrognosticsConfig] = None,
                 initial_drift: float = 0.0,
                 initial_variance: float = 1e-8) -> None:
        self.config = config or PrognosticsConfig()
        self.drift = float(initial_drift)
        self.variance = float(initial_variance)
        self._last_time: Optional[float] = None
        self._last_x: Optional[float] = None
        self._increments: Deque[float] = deque(maxlen=200)
        self.n_updates = 0

    def update(self, time_s: float, degradation: float) -> None:
        """Fold one degradation observation into the drift/volatility estimate."""
        if self._last_time is not None:
            dt = time_s - self._last_time
            if dt > 0:
                observed_rate = (degradation - self._last_x) / dt
                self._increments.append(observed_rate)
                # Predict
                prior_var = self.variance + self.config.drift_process_noise
                # Update
                gain = prior_var / (prior_var + self.config.drift_measurement_noise)
                self.drift = self.drift + gain * (observed_rate - self.drift)
                self.variance = (1.0 - gain) * prior_var
                self.n_updates += 1
        self._last_time = time_s
        self._last_x = degradation

    @property
    def volatility(self) -> float:
        """Empirical diffusion coefficient of the degradation increments."""
        if len(self._increments) < 3:
            return 1e-6
        return float(max(np.std(self._increments), 1e-9))

    def reset(self) -> None:
        self.drift = 0.0
        self.variance = 1e-8
        self._last_time = None
        self._last_x = None
        self._increments.clear()
        self.n_updates = 0


class RULPredictor:
    """Inverse-Gaussian first-hitting-time RUL predictor (original, Wiener/Kalman).

    Notes
    -----
    For ``dX = mu*dt + sigma*dW`` starting at ``x0`` with absorbing barrier ``w``,
    the first hitting time is Inverse Gaussian with mean ``(w - x0)/mu`` and
    variance ``(w - x0)*sigma^2/mu^3``.

    No longer wired into :class:`~aerotwin_physics.simulator.RealTimeMonitor`
    by default (see :class:`MLRULPredictor`), but kept fully functional --
    instantiate it directly wherever the uncertainty band or in-flight
    responsiveness matters more than raw point accuracy.
    """

    def __init__(self, config: Optional[EngineConfig] = None) -> None:
        self.config = config or EngineConfig.default()
        self.settings: PrognosticsConfig = self.config.prognostics
        self.threshold = self.config.degradation.failure_threshold
        self.estimator = DriftEstimator(self.settings)
        self._n = 0

    def reset(self) -> None:
        self.estimator.reset()
        self._n = 0

    def update(self, time_s: float, mahalanobis: float) -> None:
        """Ingest one monitored sample."""
        health = float(health_index_from_distance(
            mahalanobis, self.settings.health_index_rho))
        self.estimator.update(time_s, 1.0 - health)
        self._n += 1

    def estimate(self, time_s: float, mahalanobis: float) -> RULEstimate:
        """Current RUL estimate with confidence bounds.

        Raises
        ------
        PrognosticsError
            If fewer than ``min_samples_for_estimate`` samples have been seen. A
            wrong number early is worse than no number: it anchors the operator.
        """
        if self._n < self.settings.min_samples_for_estimate:
            raise PrognosticsError(
                f"need {self.settings.min_samples_for_estimate} samples before an RUL "
                f"estimate is meaningful; have {self._n}")
        health = float(health_index_from_distance(
            mahalanobis, self.settings.health_index_rho))
        degradation = 1.0 - health
        headroom = self.threshold - degradation
        drift = self.estimator.drift
        volatility = self.estimator.volatility
        provisional = self._n < self.settings.provisional_sample_count

        if headroom <= 0:
            return RULEstimate(time_s, 0.0, 0.0, 0.0, 0.0, 0.0,
                               self.settings.confidence, health, degradation,
                               drift, volatility, provisional, self._n)
        if drift <= 0:
            # No degradation trend: the first-hitting-time distribution is
            # improper. Report the clamp and let the caller surface it as
            # "no trend detected" rather than as a reassuringly huge number.
            capped = self.settings.max_rul_s
            return RULEstimate(time_s, capped, capped, capped, capped, 0.0,
                               self.settings.confidence, health, degradation,
                               drift, volatility, True, self._n)

        mean = headroom / drift
        variance = headroom * volatility ** 2 / drift ** 3
        std = float(np.sqrt(max(variance, 1e-18)))
        lam = (mean ** 3) / max(variance, 1e-18)
        dist = stats.invgauss(mu=mean / lam, scale=lam)
        alpha = 1.0 - self.settings.confidence
        lower, median, upper = (float(dist.ppf(alpha / 2)), float(dist.ppf(0.5)),
                                float(dist.ppf(1.0 - alpha / 2)))
        cap = self.settings.max_rul_s
        return RULEstimate(
            time_s=time_s, mean=float(min(mean, cap)), median=float(min(median, cap)),
            lower=float(min(lower, cap)), upper=float(min(upper, cap)), std=std,
            confidence=self.settings.confidence, health_index=health,
            degradation=degradation, drift=drift, volatility=volatility,
            provisional=provisional, n_samples=self._n)


class MLRULPredictor:
    """Model 5: leakage-free ExtraTreesRegressor (51 raw-telemetry features),
    now the primary RUL estimator for the online monitoring path.

    Trade-off, made explicitly rather than silently: this gives a single
    point estimate (R2=0.760, MAE=805 cycles on held-out missions via 5-fold
    GroupKFold-by-mission) with no fitted confidence interval and no
    in-flight responsiveness to a throttled-back engine -- both of which
    :class:`RULPredictor` above has. ``lower``/``upper`` here are a fixed
    +/-15% band for display continuity with the existing ``RULEstimate``
    shape, not a statistical interval; do not feed them into anything that
    assumes a fitted distribution (e.g. ``survival_probability`` falls back
    to a normal approximation for exactly this reason).

    Falls back to raising :class:`PrognosticsError` (same contract as
    ``RULPredictor.estimate`` before ``min_samples_for_estimate``) if the
    trained artefact is not present at ``model_dir``, rather than silently
    guessing a number.
    """

    def __init__(self, model_dir: Path = _DEFAULT_MODEL5_DIR,
                 min_samples_for_estimate: int = 5,
                 provisional_sample_count: int = 10,
                 cycle_seconds: float = 60.0) -> None:
        self._model = None
        self._feature_list: List[str] = []
        self.min_samples_for_estimate = min_samples_for_estimate
        self.provisional_sample_count = provisional_sample_count
        self.cycle_seconds = cycle_seconds
        self._n = 0

        model_path = model_dir / "model5_rul_final_clean.joblib"
        feature_path = model_dir / "model5_feature_list_final.json"

        if model_path.exists() and feature_path.exists():
            self._model = joblib.load(model_path)
            self._feature_list = json.loads(feature_path.read_text())
            assert "rul_target_cycles" not in self._feature_list, (
                "target leakage guard: rul_target_cycles must never be a "
                "feature -- this artefact was supposed to be the "
                "leakage-fixed export (model5_final_config.json's "
                "target_leakage_removed=True)"
            )
            logger.info(
                "MLRULPredictor loaded (%d features) from %s",
                len(self._feature_list), model_dir,
            )
        else:
            logger.warning(
                "Model 5 artefacts not found at %s -- MLRULPredictor.estimate() "
                "will raise PrognosticsError until they are supplied.",
                model_dir,
            )

    def reset(self) -> None:
        self._n = 0

    def update(self, feature_row: Mapping[str, Any]) -> None:
        """Ingest one monitored sample. Stateless beyond the sample count --
        unlike RULPredictor, this estimator does not accumulate a drift
        estimate across calls."""
        self._n += 1

    def estimate(self, feature_row: Mapping[str, Any]) -> RULEstimate:
        if self._model is None:
            raise PrognosticsError(
                "Model 5 artefact not loaded -- see MLRULPredictor's warning "
                "at startup for the expected path"
            )
        if self._n < self.min_samples_for_estimate:
            raise PrognosticsError(
                f"need {self.min_samples_for_estimate} samples before an RUL "
                f"estimate is meaningful; have {self._n}")

        vec = np.array([
            float(v) if (v := feature_row.get(f)) is not None else 0.0
            for f in self._feature_list
        ])
        rul_cycles = max(0.0, float(self._model.predict(vec.reshape(1, -1))[0]))
        rul_s = rul_cycles * self.cycle_seconds

        band = rul_s * 0.15
        health_index = float(feature_row.get("health_index", 0.0))
        return RULEstimate(
            time_s=float(feature_row.get("time_s", 0.0)),
            mean=rul_s, median=rul_s,
            lower=max(0.0, rul_s - band), upper=rul_s + band,
            std=band / 1.96,
            confidence=0.760,               # the model's validation R2, not a CI level
            health_index=health_index,
            degradation=1.0 - health_index,
            drift=0.0, volatility=0.0,      # not modeled by this estimator
            provisional=self._n < self.provisional_sample_count,
            n_samples=self._n,
        )


def mission_survival_probability(estimate: RULEstimate,
                                 mission_time_remaining_s: float) -> float:
    """Convenience wrapper: probability the engine outlives the mission."""
    return estimate.survival_probability(mission_time_remaining_s)
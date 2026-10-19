"""Fault isolation: physics-informed fingerprinting with an optional ML cross-check.

The residual engine answers *is something wrong*. This module answers *what*.

Primary method: **cosine similarity against a physics-derived Fault Signature
Matrix**. A degrading component does not push the residual in a random direction;
it pushes it in a specific, predictable one. Comparing the normalised residual
against each fault's signature isolates the fault *type* independently of its
*severity*, which is what makes zero-shot diagnosis possible - the first time a
wastegate leak ever occurs on this airframe, the signature is already known from
physics rather than needing training examples.

Secondary method: a supervised cross-check (RandomForest by default, matching the
reference dataset's own estimator). It corroborates; it does not override. With
only 2-3 trajectories per fault type in the reference data, per-class held-out
accuracy runs 13-62%. Those numbers are honest and they are the reason a
data-driven classifier cannot be the primary isolator here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from .config import CHANNELS, UNKNOWN_CHANNELS, DiagnosticsConfig, EngineConfig
from .exceptions import AeroTwinError
from .faults import FaultLibrary
from .logging_utils import get_logger

logger = get_logger(__name__)

NO_FAULT = "none"

#: Pseudo-mode for distributed, non-localised deterioration.
#:
#: Slow wear is not a discrete fault, but it *does* have a measured residual
#: direction (``FaultConfig.degradation_gains``). Without it as a competing
#: hypothesis the isolator has nowhere to put a wear-dominated residual and is
#: forced to assign it to whichever discrete fault happens to point most nearly
#: the same way - producing a confident, specific, wrong diagnosis on an engine
#: that is merely old. Including it converts that failure into the correct and
#: actionable answer: "general deterioration, no discrete fault isolated".
GENERALISED_WEAR = "generalised_wear"


@dataclass
class Diagnosis:
    """Isolation result for one residual sample."""
    fault: str
    confidence: float
    similarities: Dict[str, float]
    method: str
    #: Channels that drove the match, for the operator-facing explanation.
    evidence: List[Tuple[str, float]] = field(default_factory=list)
    ml_agreement: Optional[bool] = None

    def explain(self) -> str:
        """One-line justification suitable for a GCS alert banner."""
        if self.fault == NO_FAULT:
            return "No fault signature matched above the isolation threshold."
        drivers = ", ".join(f"{c} ({v:+.1%})" for c, v in self.evidence[:3])
        return (f"Residual aligned {self.similarities.get(self.fault, 0):.0%} with the "
                f"{self.fault.replace('_', ' ')} signature; driven by {drivers}.")

    def as_dict(self) -> Dict[str, Any]:
        out = {"diagnosed_fault": self.fault, "diagnosis_confidence": self.confidence,
               "diagnosis_method": self.method}
        for mode, sim in self.similarities.items():
            out[f"{mode}_similarity"] = sim
        return out


class FaultFingerprinter:
    """Cosine-similarity isolation against the Fault Signature Matrix."""

    def __init__(self, config: Optional[EngineConfig] = None,
                 library: Optional[FaultLibrary] = None) -> None:
        self.config = config or EngineConfig.default()
        self.settings: DiagnosticsConfig = self.config.diagnostics
        self.library = library or FaultLibrary(self.config)
        self._modes, matrix = self.library.signature_matrix()
        # Append generalised wear as a competing hypothesis, using its own
        # measured channel sensitivities.
        wear = np.array([self.config.faults.degradation_gains.get(c, 0.0)
                         for c in CHANNELS], float)
        if np.linalg.norm(wear) > 1e-12:
            self._modes = list(self._modes) + [GENERALISED_WEAR]
            matrix = np.vstack([matrix, wear])
        # Signatures are relative deviations; residuals are in raw channel units.
        # Converting a signature to raw units means multiplying by channel
        # magnitude - but comparing in *raw* units lets a large-magnitude, noisy
        # channel dominate the cosine regardless of how much information it
        # carries. RPM (~2330 units, sigma 28) would swamp oil_pressure (~74
        # units, sigma 0.7) and every residual would match whichever fault
        # touches RPM.
        #
        # So the comparison is done in the *whitened* space: divide by each
        # channel's noise sigma, so one unit of distance means one standard
        # deviation on every channel. This is the same metric the Mahalanobis
        # detector uses, which keeps detection and isolation mutually consistent.
        magnitude = np.array([self.config.noise.reference_scale.get(c, 1.0)
                              for c in CHANNELS], float)
        sigma = np.array([
            max(self.config.noise.relative_sigma.get(c, 0.01)
                * self.config.noise.reference_scale.get(c, 1.0), 1e-9)
            for c in CHANNELS], float)
        weights = np.array([self.settings.unknown_channel_weight
                            if c in UNKNOWN_CHANNELS else 1.0 for c in CHANNELS], float)
        self._magnitude = magnitude
        self._sigma = sigma
        self._weights = weights
        # gain -> raw units (x magnitude) -> whitened (/ sigma) -> weighted
        self._signatures = matrix * magnitude / sigma * weights
        norms = np.linalg.norm(self._signatures, axis=1, keepdims=True)
        self._unit_signatures = self._signatures / np.maximum(norms, 1e-12)

    @property
    def modes(self) -> List[str]:
        return list(self._modes)

    def similarities(self, residual: np.ndarray,
                     valid_mask: Optional[np.ndarray] = None) -> Dict[str, float]:
        """Cosine similarity of a residual against each fault signature."""
        vector = np.asarray(residual, float) / self._sigma * self._weights
        signatures = self._unit_signatures
        if valid_mask is not None and not bool(np.all(valid_mask)):
            mask = np.asarray(valid_mask, bool)
            vector = vector * mask
            signatures = signatures * mask
            norms = np.linalg.norm(signatures, axis=1, keepdims=True)
            signatures = signatures / np.maximum(norms, 1e-12)
        norm = np.linalg.norm(vector)
        if norm < 1e-12:
            return {m: 0.0 for m in self._modes}
        sims = signatures @ (vector / norm)
        return {m: float(s) for m, s in zip(self._modes, sims)}

    def _softmax_confidence(self, sims: Mapping[str, float], winner: str) -> float:
        temperature = max(self.settings.softmax_temperature, 1e-6)
        values = np.array(list(sims.values()), float) / temperature
        values -= values.max()
        weights = np.exp(values)
        probs = weights / weights.sum()
        return float(probs[list(sims).index(winner)])

    def diagnose(self, residual: np.ndarray,
                 valid_mask: Optional[np.ndarray] = None,
                 anomaly_flag: bool = True) -> Diagnosis:
        """Isolate the most likely fault mode.

        Runs only when the anomaly detector has tripped. Isolating continuously
        wastes ground-station compute and, worse, produces a confident fault label
        for pure noise - which is how operators learn to ignore the panel.
        """
        sims = self.similarities(residual, valid_mask)
        if not anomaly_flag:
            return Diagnosis(NO_FAULT, 0.0, sims, "not_evaluated_no_anomaly")
        winner = max(sims, key=sims.get)
        best = sims[winner]
        if best < self.settings.min_similarity:
            return Diagnosis(NO_FAULT, float(max(0.0, best)), sims,
                             "cosine_fsm_below_threshold")
        confidence = self._softmax_confidence(sims, winner)
        contribution = (np.asarray(residual, float) / self._sigma) * \
            self._unit_signatures[self._modes.index(winner)]
        order = np.argsort(-np.abs(contribution))
        evidence = [(CHANNELS[i],
                     float(residual[i] / max(abs(self._magnitude[i]), 1e-9)))
                    for i in order[:4]]
        return Diagnosis(winner, confidence, sims, "cosine_fsm_physics_signature",
                         evidence=evidence)


class SupervisedCrossCheck:
    """Optional RandomForest corroboration of the physics-based diagnosis.

    Mirrors the reference dataset's own estimator and validation protocol
    (``RandomForestClassifier`` with leave-one-trajectory-out / GroupKFold), so
    reported accuracies are comparable to the ones documented there. Group-aware
    splitting is not optional: with 50 consecutive samples per trajectory, a
    random split leaks near-identical neighbours across the fold boundary and
    reports accuracy that evaporates in flight.
    """

    def __init__(self, config: Optional[EngineConfig] = None,
                 n_estimators: int = 200, random_state: int = 42) -> None:
        self.config = config or EngineConfig.default()
        self.n_estimators = n_estimators
        self.random_state = random_state
        self._model = None
        self._classes: List[str] = []

    @property
    def is_fitted(self) -> bool:
        return self._model is not None

    def fit(self, residuals: np.ndarray, labels: Sequence[str]) -> "SupervisedCrossCheck":
        """Train on residual vectors with fault labels."""
        try:
            from sklearn.ensemble import RandomForestClassifier
        except ImportError as exc:  # pragma: no cover
            raise AeroTwinError("scikit-learn is required for the ML cross-check") from exc
        residuals = np.asarray(residuals, float)
        if residuals.shape[0] != len(labels):
            raise AeroTwinError("residuals and labels must have equal length")
        self._model = RandomForestClassifier(
            n_estimators=self.n_estimators, random_state=self.random_state,
            class_weight="balanced_subsample", min_samples_leaf=2)
        self._model.fit(residuals, list(labels))
        self._classes = list(self._model.classes_)
        logger.info("cross-check trained on %d samples, %d classes",
                    residuals.shape[0], len(self._classes))
        return self

    def predict(self, residual: np.ndarray) -> Tuple[str, float]:
        if not self.is_fitted:
            raise AeroTwinError("cross-check model is not fitted")
        probs = self._model.predict_proba(np.asarray(residual, float).reshape(1, -1))[0]
        idx = int(np.argmax(probs))
        return self._classes[idx], float(probs[idx])

    def cross_validate(self, residuals: np.ndarray, labels: Sequence[str],
                       groups: Sequence[Any]) -> Dict[str, Any]:
        """Leave-one-trajectory-out accuracy, overall and per class."""
        try:
            from sklearn.ensemble import RandomForestClassifier
            from sklearn.model_selection import LeaveOneGroupOut
        except ImportError as exc:  # pragma: no cover
            raise AeroTwinError("scikit-learn is required for cross-validation") from exc
        residuals = np.asarray(residuals, float)
        labels = np.asarray(labels)
        groups = np.asarray(groups)
        predictions = np.empty_like(labels)
        for train_idx, test_idx in LeaveOneGroupOut().split(residuals, labels, groups):
            model = RandomForestClassifier(
                n_estimators=self.n_estimators, random_state=self.random_state,
                class_weight="balanced_subsample", min_samples_leaf=2)
            model.fit(residuals[train_idx], labels[train_idx])
            predictions[test_idx] = model.predict(residuals[test_idx])
        per_class = {c: float((predictions[labels == c] == c).mean())
                     for c in np.unique(labels)}
        return {"overall_accuracy": float((predictions == labels).mean()),
                "per_class_accuracy": per_class,
                "n_groups": int(len(np.unique(groups))),
                "protocol": "leave_one_trajectory_out"}


class DiagnosticEngine:
    """Physics-first isolation with optional supervised corroboration."""

    def __init__(self, config: Optional[EngineConfig] = None,
                 library: Optional[FaultLibrary] = None) -> None:
        self.config = config or EngineConfig.default()
        self.fingerprinter = FaultFingerprinter(self.config, library)
        self.crosscheck = SupervisedCrossCheck(self.config) \
            if self.config.diagnostics.enable_ml_crosscheck else None

    def diagnose(self, residual: np.ndarray, anomaly_flag: bool = True,
                 valid_mask: Optional[np.ndarray] = None) -> Diagnosis:
        result = self.fingerprinter.diagnose(residual, valid_mask, anomaly_flag)
        if self.crosscheck is not None and self.crosscheck.is_fitted \
                and result.fault != NO_FAULT:
            predicted, _ = self.crosscheck.predict(residual)
            result.ml_agreement = bool(predicted == result.fault)
            if not result.ml_agreement:
                logger.debug("cross-check disagrees: physics=%s ml=%s",
                             result.fault, predicted)
        return result

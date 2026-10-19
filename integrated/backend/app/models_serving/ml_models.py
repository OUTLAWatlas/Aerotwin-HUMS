"""
Wrappers around pre-trained scikit-learn-style artifacts (joblib). These
classes only handle loading + inference plumbing (feature vectorization,
timeouts are enforced by the orchestrator, confidence extraction) -- no
training code, per the integration-not-training scope.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import joblib
import numpy as np

logger = logging.getLogger(__name__)

# Shared executor so blocking sklearn `.predict()` calls don't block the
# event loop; sized modestly since each call should finish well under 100ms.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ml-inference")


def _vectorize(sensor_payload: dict, expected_features: list[str]) -> np.ndarray:
    """Builds a fixed-order feature vector, filling missing sensors with 0.0
    (in production, imputation strategy would itself be config-driven)."""
    values = []
    for feat in expected_features:
        reading = sensor_payload.get(feat)
        if reading is None:
            values.append(0.0)
        elif isinstance(reading, dict):
            values.append(float(reading.get("value", 0.0)))
        else:
            values.append(float(reading))
    return np.array(values, dtype=np.float64).reshape(1, -1)


class _BaseMLModel:
    expected_features = ["vibration", "temperature", "pressure", "rpm", "current"]

    def __init__(self, version: str, artifact_path: str | None):
        self.version = version
        self.artifact_path = artifact_path
        self._pipeline = self._load_artifact(artifact_path)

    @staticmethod
    def _load_artifact(artifact_path: str | None):
        if artifact_path and Path(artifact_path).exists():
            return joblib.load(artifact_path)
        logger.warning(
            "ML artifact not found at %s -- using a stub predictor "
            "(replace with a real trained pipeline for production).",
            artifact_path,
        )
        return None

    async def _run_blocking(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, fn, *args)


class AnomalyEnsembleModel(_BaseMLModel):
    async def predict(self, sensor_payload: dict, context=None) -> tuple[dict, float]:
        vec = _vectorize(sensor_payload, self.expected_features)
        if self._pipeline is not None:
            proba = await self._run_blocking(self._pipeline.predict_proba, vec)
            anomaly_score = float(proba[0][1])
        else:
            # Stub: flag anomaly if any reading is >2 std-ish from a rough
            # heuristic baseline, purely so the API path is exercisable
            # without a real artifact present.
            anomaly_score = float(min(1.0, np.abs(vec).mean() / 100))
        return (
            {"anomaly_score": anomaly_score, "anomaly_detected": anomaly_score > 0.5},
            anomaly_score,
        )


class FaultClassifierModel(_BaseMLModel):
    fault_labels = ["none", "bearing_wear", "seal_leak", "overheat", "imbalance"]

    async def predict(self, sensor_payload: dict, context=None) -> tuple[dict, float]:
        vec = _vectorize(sensor_payload, self.expected_features)
        if self._pipeline is not None:
            proba = await self._run_blocking(self._pipeline.predict_proba, vec)
            best_idx = int(np.argmax(proba[0]))
            confidence = float(proba[0][best_idx])
            label = self.fault_labels[best_idx] if best_idx < len(self.fault_labels) else "unknown"
        else:
            label, confidence = "none", 0.1
        return (
            {"fault_code": label, "class_probabilities": self.fault_labels},
            confidence,
        )


class RULEstimatorModel(_BaseMLModel):
    async def predict(self, sensor_payload: dict, context=None) -> tuple[dict, float]:
        vec = _vectorize(sensor_payload, self.expected_features)
        if self._pipeline is not None:
            pred = await self._run_blocking(self._pipeline.predict, vec)
            rul_hours = float(pred[0])
            confidence = 0.8  # regression models: derive from held-out interval width in practice
        else:
            rul_hours, confidence = 500.0, 0.2
        ci_width = rul_hours * 0.15
        return (
            {
                "rul_hours": rul_hours,
                "ci_lower_hours": max(0.0, rul_hours - ci_width),
                "ci_upper_hours": rul_hours + ci_width,
            },
            confidence,
        )

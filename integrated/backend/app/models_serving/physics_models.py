"""
Physics-based residual model: compares observed sensor values against a
first-principles expected value and flags deviations. Pre-trained here means
the physical parameters (coefficients, thresholds) are pre-calibrated and
loaded from a JSON artifact rather than fit at request time.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path


class PhysicsResidualModel:
    def __init__(self, version: str, artifact_path: str | None):
        self.version = version
        self._params = self._load_params(artifact_path)

    @staticmethod
    def _load_params(artifact_path: str | None) -> dict:
        if artifact_path and Path(artifact_path).exists():
            with open(artifact_path) as f:
                return json.load(f)
        # Sensible defaults if the calibration artifact isn't present
        # (e.g. in a fresh dev environment) -- production always ships
        # the real calibrated artifact.
        return {
            "vibration": {"expected_baseline": 2.0, "residual_threshold": 1.5},
            "temperature": {"expected_baseline": 70.0, "residual_threshold": 25.0},
            "pressure": {"expected_baseline": 100.0, "residual_threshold": 30.0},
        }

    async def predict(self, sensor_payload: dict, context=None) -> tuple[dict, float]:
        # Physics residual calc is cheap CPU work; yield control once so the
        # event loop stays responsive under concurrent requests.
        await asyncio.sleep(0)

        residuals = {}
        max_normalized_residual = 0.0
        for sensor_type, reading in sensor_payload.items():
            params = self._params.get(sensor_type)
            if not params:
                continue
            value = reading["value"] if isinstance(reading, dict) else reading
            residual = abs(value - params["expected_baseline"])
            normalized = residual / params["residual_threshold"]
            residuals[sensor_type] = {
                "residual": residual,
                "normalized_residual": normalized,
                "exceeds_threshold": normalized > 1.0,
            }
            max_normalized_residual = max(max_normalized_residual, normalized)

        # Confidence heuristic: further past threshold => more confident
        # something real is happening (capped at 0.95).
        confidence = min(0.95, max_normalized_residual / 2) if residuals else 0.0

        return (
            {
                "residuals": residuals,
                "anomaly_detected": max_normalized_residual > 1.0,
                "max_normalized_residual": max_normalized_residual,
            },
            confidence,
        )

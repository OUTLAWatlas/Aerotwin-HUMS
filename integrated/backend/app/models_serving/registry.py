"""
Model registry: loads all models declared in config/model_routing.yaml at
application startup, caches them in memory, and exposes a uniform async
`predict()` interface regardless of whether the underlying model is an
ML artifact, a physics calculator, or a stateful physics engine (AeroTwin).
"""
from __future__ import annotations

import importlib
import logging
import time
from dataclasses import dataclass
from typing import Optional, Protocol

import yaml

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class PredictionContext:
    """Everything a model call needs beyond the raw sensor payload.

    Stateless ML/physics models ignore this. Stateful engines (AeroTwin) need
    mission_id + engine_id to manage their own session lifecycle, and
    operating_point for the physics inputs that aren't sensor *readings*
    (altitude, throttle position, etc).
    """
    mission_id: str
    drone_id: str
    engine_id: str
    drone_type: str
    operating_point: Optional[dict] = None


class InferenceModel(Protocol):
    version: str

    async def predict(self, sensor_payload: dict, context: PredictionContext) -> tuple[dict, float]:
        """Returns (output_dict, confidence_score)."""
        ...


class ModelRegistry:
    def __init__(self):
        self._models: dict[str, InferenceModel] = {}
        self._load_times_ms: dict[str, float] = {}

    def load_all(self, config_path=None) -> None:
        config_path = config_path or settings.model_routing_config_path
        with open(config_path) as f:
            raw = yaml.safe_load(f)

        for name, spec in raw["models"].items():
            start = time.perf_counter()
            module = importlib.import_module(spec["module"])
            model_cls = getattr(module, spec["entrypoint"])
            instance = model_cls(
                version=spec["version"],
                artifact_path=spec.get("artifact_path"),
            )
            self._models[name] = instance
            self._load_times_ms[name] = (time.perf_counter() - start) * 1000
            logger.info(
                "Loaded model '%s' (version=%s) in %.1fms",
                name, spec["version"], self._load_times_ms[name],
            )

    def get(self, name: str) -> InferenceModel | None:
        return self._models.get(name)

    def list_loaded(self) -> dict[str, str]:
        return {name: model.version for name, model in self._models.items()}


# Process-wide singleton, populated in main.py's startup event
model_registry = ModelRegistry()

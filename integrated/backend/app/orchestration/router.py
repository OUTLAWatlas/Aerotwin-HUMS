"""
Model orchestration: given normalized sensor data + mission context, resolves
the applicable routing rule and executes the model sequence, applying the
100ms-per-call timeout budget and fallback semantics.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.models_serving.registry import ModelRegistry, PredictionContext
from app.orchestration.rules import ModelStep, get_routing_config

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class ModelCallResult:
    model_name: str
    model_version: str
    task: str
    output: dict
    confidence: float
    execution_time_ms: float
    used_fallback: bool = False
    degraded: bool = False


@dataclass
class OrchestrationResult:
    mission_id: str
    drone_id: str
    engine_id: str
    drone_type: str
    rule_used: str
    results: list[ModelCallResult] = field(default_factory=list)
    total_execution_time_ms: float = 0.0


class OrchestrationEngine:
    def __init__(self, registry: ModelRegistry):
        self.registry = registry
        self.routing_config = get_routing_config(settings.model_routing_config_path)

    async def run(
        self,
        mission_id: str,
        drone_id: str,
        engine_id: str,
        drone_type: str,
        sensor_payload: dict[str, Any],
        risk_tier: str,
        operating_point: dict | None = None,
    ) -> OrchestrationResult:
        available_sensor_types = set(sensor_payload.keys())
        rule = self.routing_config.resolve(available_sensor_types, risk_tier, drone_type)

        context = PredictionContext(
            mission_id=mission_id,
            drone_id=drone_id,
            engine_id=engine_id,
            drone_type=drone_type,
            operating_point=operating_point,
        )

        result = OrchestrationResult(
            mission_id=mission_id,
            drone_id=drone_id,
            engine_id=engine_id,
            drone_type=drone_type,
            rule_used=rule.name,
        )
        start_total = time.perf_counter()

        for step in rule.sequence:
            call_result = await self._execute_step(step, sensor_payload, context)
            result.results.append(call_result)

        result.total_execution_time_ms = (time.perf_counter() - start_total) * 1000
        return result

    async def _execute_step(
        self, step: ModelStep, sensor_payload: dict, context: PredictionContext
    ) -> ModelCallResult:
        primary_result = await self._call_model_with_timeout(
            step.model, step.task, sensor_payload, context
        )

        if primary_result is None:
            # Timeout or hard failure -- try fallback if configured
            if step.fallback:
                logger.warning("%s failed/timed out, falling back to %s", step.model, step.fallback)
                fallback_result = await self._call_model_with_timeout(
                    step.fallback, step.task, sensor_payload, context
                )
                if fallback_result is not None:
                    fallback_result.used_fallback = True
                    return fallback_result
            return ModelCallResult(
                model_name=step.model,
                model_version="unknown",
                task=step.task,
                output={},
                confidence=0.0,
                execution_time_ms=settings.inference_timeout_seconds * 1000,
                degraded=True,
            )

        if primary_result.confidence < step.on_low_confidence_below and step.fallback:
            logger.info(
                "%s low confidence (%.2f < %.2f), trying fallback %s",
                step.model, primary_result.confidence, step.on_low_confidence_below, step.fallback,
            )
            fallback_result = await self._call_model_with_timeout(
                step.fallback, step.task, sensor_payload, context
            )
            if fallback_result and fallback_result.confidence > primary_result.confidence:
                fallback_result.used_fallback = True
                return fallback_result

        return primary_result

    async def _call_model_with_timeout(
        self, model_name: str, task: str, sensor_payload: dict, context: PredictionContext
    ) -> ModelCallResult | None:
        model = self.registry.get(model_name)
        if model is None:
            logger.error("Model '%s' not found in registry", model_name)
            return None

        start = time.perf_counter()
        try:
            output, confidence = await asyncio.wait_for(
                model.predict(sensor_payload, context), timeout=settings.inference_timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.error("Model '%s' exceeded %sms latency budget", model_name,
                         settings.inference_timeout_seconds * 1000)
            return None
        except Exception:
            logger.exception("Model '%s' raised an exception during inference", model_name)
            return None

        exec_ms = (time.perf_counter() - start) * 1000
        return ModelCallResult(
            model_name=model_name,
            model_version=model.version,
            task=task,
            output=output,
            confidence=confidence,
            execution_time_ms=exec_ms,
        )

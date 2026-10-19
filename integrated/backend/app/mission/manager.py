"""
Mission/flight configuration management + risk assessment, tied to a drone
and its engine configuration. Risk tiering feeds directly into the
orchestration router's rule matching (which also matches on drone_type).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import HealthIndex, Mission
from app.mission.drone_registry import get_drone_registry

settings = get_settings()

# Config-driven risk weighting. In production this table would live in
# config/ alongside model_routing.yaml so risk policy can change without a
# code deploy.
RISK_TOLERANCE_BASE_SCORE = {"low": 0.2, "medium": 0.5, "high": 0.8}

CONDITION_RISK_MODIFIERS = {
    "high_altitude": 0.1,
    "extreme_temperature": 0.15,
    "high_vibration_environment": 0.15,
    "night_operation": 0.05,
    "adverse_weather": 0.2,
}

# Dual-engine drones carry a small additional baseline risk modifier: losing
# one of two engines is more recoverable than a single-engine drone losing
# its only engine, but cross-engine vibration coupling adds diagnostic
# uncertainty -- reflected here rather than hardcoded into the router.
DRONE_TYPE_RISK_MODIFIERS = {
    "dual_engine": 0.05,
    "single_engine": 0.10,
}


def compute_risk_score(
    risk_tolerance: str,
    drone_type: str,
    operating_conditions: dict | None,
    current_health_scores: list[float] | None = None,
) -> float:
    base = RISK_TOLERANCE_BASE_SCORE.get(risk_tolerance, 0.5)
    modifier = DRONE_TYPE_RISK_MODIFIERS.get(drone_type, 0.0)
    for condition, active in (operating_conditions or {}).items():
        if active and condition in CONDITION_RISK_MODIFIERS:
            modifier += CONDITION_RISK_MODIFIERS[condition]

    health_penalty = 0.0
    if current_health_scores:
        avg_health = sum(current_health_scores) / len(current_health_scores)
        health_penalty = (1 - avg_health) * 0.3  # degraded health raises risk

    return min(1.0, base + modifier + health_penalty)


def risk_score_to_tier(score: float) -> str:
    if score >= 0.75:
        return "critical"
    if score >= 0.5:
        return "high"
    if score >= 0.25:
        return "medium"
    return "low"


class MissionManager:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.drone_registry = get_drone_registry(settings.drone_config_path)

    async def create_or_update_mission(
        self,
        mission_id: str,
        drone_id: str,
        flight_duration_planned_min: int | None,
        operating_conditions: dict | None,
        risk_tolerance: str,
    ) -> Mission:
        drone_type = self.drone_registry.get_drone_type(drone_id)
        if drone_type is None:
            raise ValueError(f"Unknown drone_id '{drone_id}' -- not in drone_configs.yaml")

        current_health_scores = await self._latest_health_scores(drone_id)
        risk_score = compute_risk_score(
            risk_tolerance, drone_type, operating_conditions, current_health_scores
        )

        existing = await self.session.get(Mission, mission_id)
        if existing:
            existing.drone_id = drone_id
            existing.drone_type = drone_type
            existing.flight_duration_planned_min = flight_duration_planned_min
            existing.operating_conditions = operating_conditions
            existing.risk_tolerance = risk_tolerance
            existing.risk_score = risk_score
            existing.updated_at = datetime.now(timezone.utc)
            mission = existing
        else:
            mission = Mission(
                mission_id=mission_id,
                drone_id=drone_id,
                drone_type=drone_type,
                flight_duration_planned_min=flight_duration_planned_min,
                operating_conditions=operating_conditions,
                risk_tolerance=risk_tolerance,
                risk_score=risk_score,
            )
            self.session.add(mission)

        await self.session.commit()
        await self.session.refresh(mission)
        return mission

    async def get_mission(self, mission_id: str) -> Mission | None:
        result = await self.session.execute(select(Mission).where(Mission.mission_id == mission_id))
        return result.scalar_one_or_none()

    async def get_risk_tier(self, mission_id: str) -> str:
        mission = await self.get_mission(mission_id)
        if mission is None or mission.risk_score is None:
            return "medium"  # safe default
        return risk_score_to_tier(mission.risk_score)

    async def get_drone_type_for_mission(self, mission_id: str) -> str:
        mission = await self.get_mission(mission_id)
        if mission is None:
            return "single_engine"  # safe default -- forces the conservative rule set
        return mission.drone_type

    async def _latest_health_scores(self, drone_id: str) -> list[float]:
        engine_ids = self.drone_registry.get_engines_for_drone(drone_id)
        if not engine_ids:
            return []
        scores = []
        for engine_id in engine_ids:
            result = await self.session.execute(
                select(HealthIndex.health_score)
                .where(HealthIndex.engine_id == engine_id)
                .order_by(HealthIndex.time.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row is not None:
                scores.append(row)
        return scores

    async def compare_engine_health(self, drone_id: str) -> list[dict]:
        """Multi-engine correlation: returns the latest health index per
        engine on a drone, so dashboards can compare engines side by side
        (e.g. spotting one engine on a dual-engine drone degrading faster
        than its twin)."""
        engine_ids = self.drone_registry.get_engines_for_drone(drone_id)
        comparison = []
        for engine_id in engine_ids:
            result = await self.session.execute(
                select(HealthIndex)
                .where(HealthIndex.engine_id == engine_id)
                .order_by(HealthIndex.time.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            comparison.append({
                "engine_id": engine_id,
                "health_score": row.health_score if row else None,
                "as_of": row.time.isoformat() if row else None,
                "model_version": row.model_version if row else None,
            })
        return comparison

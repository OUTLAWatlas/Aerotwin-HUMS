"""
Loads and evaluates config/model_routing.yaml. Pure logic, no I/O to models
or DB -- keeps the decision layer testable and hot-reloadable.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class ModelStep:
    model: str
    task: str
    on_low_confidence_below: float
    fallback: Optional[str]


@dataclass
class RoutingRule:
    name: str
    required_sensor_types: list[str]
    mission_risk_tiers: list[str]
    drone_types: list[str]
    sequence: list[ModelStep]

    def matches(self, available_sensor_types: set[str], risk_tier: str, drone_type: str) -> bool:
        if risk_tier not in self.mission_risk_tiers:
            return False
        if drone_type not in self.drone_types:
            return False
        # empty required_sensor_types = catch-all rule
        if not self.required_sensor_types:
            return True
        return set(self.required_sensor_types).issubset(available_sensor_types)


class RoutingConfig:
    def __init__(self, path: Path):
        with open(path) as f:
            raw = yaml.safe_load(f)
        self.rules: list[RoutingRule] = [
            RoutingRule(
                name=r["name"],
                required_sensor_types=r["when"]["required_sensor_types"],
                mission_risk_tiers=r["when"]["mission_risk_tier"],
                drone_types=r["when"]["drone_types"],
                sequence=[
                    ModelStep(
                        model=s["model"],
                        task=s["task"],
                        on_low_confidence_below=s["on_low_confidence_below"],
                        fallback=s.get("fallback"),
                    )
                    for s in r["sequence"]
                ],
            )
            for r in raw["rules"]
        ]
        self.models: dict = raw["models"]

    def resolve(self, available_sensor_types: set[str], risk_tier: str, drone_type: str) -> RoutingRule:
        for rule in self.rules:
            if rule.matches(available_sensor_types, risk_tier, drone_type):
                return rule
        raise LookupError(
            f"No routing rule matches sensors={available_sensor_types} "
            f"risk={risk_tier} drone_type={drone_type}"
        )


_cached_config: RoutingConfig | None = None


def get_routing_config(path: Path, force_reload: bool = False) -> RoutingConfig:
    global _cached_config
    if _cached_config is None or force_reload:
        _cached_config = RoutingConfig(path)
    return _cached_config

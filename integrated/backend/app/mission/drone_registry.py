"""
Loads config/drone_configs.yaml and exposes lookups from engine_id/drone_id
to drone type and engine metadata. This is the single source of truth for
drone/engine topology -- no drone/engine logic is hardcoded elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class EngineInfo:
    engine_id: str
    drone_id: str
    drone_type: str
    engine_type: str
    position: str
    can_arbitration_id_base: int | None = None


class DroneRegistry:
    def __init__(self, path: Path):
        with open(path) as f:
            raw = yaml.safe_load(f)

        self._engine_to_info: dict[str, EngineInfo] = {}
        self._drone_to_engines: dict[str, list[str]] = {}
        self._drone_to_type: dict[str, str] = {}
        self._arbitration_ranges: list[tuple[int, int, str]] = []  # (base, base+0xF, engine_id)

        for drone in raw["drones"]:
            drone_id = drone["drone_id"]
            drone_type = drone["drone_type"]
            self._drone_to_type[drone_id] = drone_type
            engine_ids = []
            for engine in drone["engines"]:
                base = engine.get("can_arbitration_id_base")
                info = EngineInfo(
                    engine_id=engine["engine_id"],
                    drone_id=drone_id,
                    drone_type=drone_type,
                    engine_type=drone["engine_type"],
                    position=engine.get("position", "unknown"),
                    can_arbitration_id_base=base,
                )
                self._engine_to_info[engine["engine_id"]] = info
                engine_ids.append(engine["engine_id"])
                if base is not None:
                    self._arbitration_ranges.append((base, base + 0xF, engine["engine_id"]))
            self._drone_to_engines[drone_id] = engine_ids

    def get_engine_info(self, engine_id: str) -> EngineInfo | None:
        return self._engine_to_info.get(engine_id)

    def get_drone_type(self, drone_id: str) -> str | None:
        return self._drone_to_type.get(drone_id)

    def get_engines_for_drone(self, drone_id: str) -> list[str]:
        return self._drone_to_engines.get(drone_id, [])

    def is_multi_engine(self, drone_id: str) -> bool:
        return len(self.get_engines_for_drone(drone_id)) > 1

    def resolve_engine_by_arbitration_id(self, arbitration_id: int) -> EngineInfo | None:
        """Maps a raw CAN arbitration ID to the engine that owns it, via the
        per-engine id ranges declared in drone_configs.yaml -- avoids any
        hardcoded if/else chain on frame IDs in the adapter itself."""
        for lo, hi, engine_id in self._arbitration_ranges:
            if lo <= arbitration_id <= hi:
                return self._engine_to_info[engine_id]
        return None


_cached_registry: DroneRegistry | None = None


def get_drone_registry(path: Path, force_reload: bool = False) -> DroneRegistry:
    global _cached_registry
    if _cached_registry is None or force_reload:
        _cached_registry = DroneRegistry(path)
    return _cached_registry

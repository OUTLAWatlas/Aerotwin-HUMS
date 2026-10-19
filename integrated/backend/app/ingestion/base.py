"""
Shared ingestion primitives: normalized reading schema, validation,
decompression, out-of-order handling, and a backpressure-aware async queue.
"""
from __future__ import annotations

import asyncio
import zlib
from datetime import datetime, timezone
from typing import Any, Optional

import zstandard as zstd
from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------------------- #
# Normalized internal representation, produced by every protocol adapter
# --------------------------------------------------------------------------- #
class NormalizedReading(BaseModel):
    mission_id: str
    drone_id: str
    engine_id: str
    drone_type: str  # "dual_engine" | "single_engine" -- resolved via DroneRegistry
    sensor_id: str
    sensor_type: str
    value: float
    unit: str
    timestamp: datetime  # normalized to UTC
    sequence_no: Optional[int] = None  # for out-of-order detection
    source_protocol: str  # "can" | "mqtt"

    @field_validator("timestamp")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        return v.astimezone(timezone.utc) if v.tzinfo else v.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Schema / range validation
# --------------------------------------------------------------------------- #
# Config-driven: sensor_type -> (min, max, expected unit). In production this
# is loaded from a DB table or YAML; kept inline here for brevity.
SENSOR_RANGES: dict[str, tuple[float, float, str]] = {
    "vibration": (0.0, 50.0, "g"),
    "temperature": (-40.0, 300.0, "C"),
    "pressure": (0.0, 500.0, "bar"),
    "rpm": (0.0, 20000.0, "rpm"),
    "current": (0.0, 1000.0, "A"),
}


class ValidationError(Exception):
    def __init__(self, reason: str, reading: dict):
        self.reason = reason
        self.reading = reading
        super().__init__(reason)


def validate_reading(reading: NormalizedReading) -> None:
    bounds = SENSOR_RANGES.get(reading.sensor_type)
    if bounds is None:
        raise ValidationError(f"unknown sensor_type '{reading.sensor_type}'", reading.model_dump())
    lo, hi, expected_unit = bounds
    if not (lo <= reading.value <= hi):
        raise ValidationError(
            f"value {reading.value} out of range [{lo}, {hi}] for {reading.sensor_type}",
            reading.model_dump(),
        )
    if reading.unit != expected_unit:
        raise ValidationError(
            f"unit mismatch: got '{reading.unit}', expected '{expected_unit}'",
            reading.model_dump(),
        )


# --------------------------------------------------------------------------- #
# Decompression
# --------------------------------------------------------------------------- #
def decompress_payload(raw: bytes, algorithm: str = "none") -> bytes:
    if algorithm == "none":
        return raw
    if algorithm == "zlib":
        return zlib.decompress(raw)
    if algorithm == "zstd":
        return zstd.ZstdDecompressor().decompress(raw)
    raise ValueError(f"unsupported compression algorithm: {algorithm}")


# --------------------------------------------------------------------------- #
# Out-of-order handling: small reordering buffer per (mission_id, sensor_id)
# --------------------------------------------------------------------------- #
class ReorderBuffer:
    """Holds recent sequence numbers per stream key and flags/reorders
    messages that arrive late, up to a bounded window."""

    def __init__(self, window: int = 50):
        self.window = window
        self._last_seq: dict[str, int] = {}

    def key(self, reading: NormalizedReading) -> str:
        # Keyed by engine_id (not just sensor_id) so simultaneous readings from
        # different engines on the same mission never collide/interleave.
        return f"{reading.mission_id}:{reading.engine_id}:{reading.sensor_id}"

    def check(self, reading: NormalizedReading) -> str:
        """Returns 'in_order', 'late', or 'ahead'."""
        if reading.sequence_no is None:
            return "in_order"  # sequence tracking not applicable
        k = self.key(reading)
        last = self._last_seq.get(k)
        if last is None or reading.sequence_no == last + 1:
            self._last_seq[k] = reading.sequence_no
            return "in_order"
        if reading.sequence_no <= last:
            return "late"  # within/behind window -- still store, flagged
        # gap ahead: accept but note the jump
        self._last_seq[k] = reading.sequence_no
        return "ahead"


# --------------------------------------------------------------------------- #
# Backpressure-aware queue shared by all adapters
# --------------------------------------------------------------------------- #
class BackpressureQueue:
    """Bounded async queue. When full, drops the oldest item (configurable)
    rather than blocking the ingestion thread indefinitely -- protects the
    adapter from an unbounded memory footprint under burst load."""

    def __init__(self, maxsize: int = 10_000, drop_policy: str = "drop_oldest"):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.drop_policy = drop_policy
        self.dropped_count = 0

    async def put(self, item: Any) -> None:
        if self._queue.full():
            if self.drop_policy == "drop_oldest":
                try:
                    self._queue.get_nowait()
                    self.dropped_count += 1
                except asyncio.QueueEmpty:
                    pass
            else:  # drop_new
                self.dropped_count += 1
                return
        await self._queue.put(item)

    async def get(self) -> Any:
        return await self._queue.get()

    def qsize(self) -> int:
        return self._queue.qsize()

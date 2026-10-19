"""
CAN bus adapter: parses raw CAN frames (via python-can + a DBC file loaded
with cantools) into NormalizedReading objects and pushes them onto the shared
backpressure queue.

Not started anywhere in app/main.py's startup, and not usable for local dev
on Windows/macOS regardless: python-can's "socketcan" bustype (the default
below) is Linux-only and needs a real CAN interface attached. The project's
default live-ingestion path is now WebSocket-based -- see
app/api/routes_ws.py's `/ws/ingest` endpoint and scripts/simulate_telemetry.py
-- which works anywhere and drives the same orchestration/alerting/broadcast
pipeline (`execute_prediction()` in routes_predictions.py).

This module is left in place for a future deployment that does have real CAN
hardware. Note that as originally written it only pushes single-channel
NormalizedReading objects onto `BackpressureQueue`, which nothing currently
drains -- there's no consumer task started in main.py. Wiring this back in
for real hardware means either (a) adding a consumer that batches each
engine's per-tick readings into one multi-channel snapshot and calls
`execute_prediction()` with it (matching what `/ws/ingest` does per
message), or (b) having this adapter build that snapshot itself and call
`execute_prediction()` directly instead of using the queue at all.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import can
import cantools

from app.ingestion.base import (
    BackpressureQueue,
    NormalizedReading,
    ReorderBuffer,
    ValidationError,
    validate_reading,
)
from app.mission.drone_registry import DroneRegistry

logger = logging.getLogger(__name__)


class CANAdapter:
    def __init__(
        self,
        queue: BackpressureQueue,
        dbc_path: Path,
        drone_registry: DroneRegistry,
        channel: str = "can0",
        bustype: str = "socketcan",
        mission_id_resolver=lambda msg: "unknown-mission",
    ):
        self.queue = queue
        self.db = cantools.database.load_file(str(dbc_path))
        self.channel = channel
        self.bustype = bustype
        self.reorder = ReorderBuffer()
        self.mission_id_resolver = mission_id_resolver
        self.drone_registry = drone_registry
        self._seq_counters: dict[int, int] = {}

    async def run(self) -> None:
        """Runs the blocking python-can reader in a thread, forwarding
        decoded frames to the async queue."""
        loop = asyncio.get_running_loop()
        bus = can.interface.Bus(channel=self.channel, bustype=self.bustype)
        try:
            while True:
                msg = await loop.run_in_executor(None, bus.recv, 1.0)  # 1s poll timeout
                if msg is None:
                    continue
                await self._handle_frame(msg)
        finally:
            bus.shutdown()

    async def _handle_frame(self, msg: can.Message) -> None:
        try:
            decoded = self.db.decode_message(msg.arbitration_id, msg.data)
        except (KeyError, cantools.database.errors.DecodeError) as exc:
            logger.warning("Unrecognized/undecodable CAN frame %s: %s", msg.arbitration_id, exc)
            return

        engine_info = self.drone_registry.resolve_engine_by_arbitration_id(msg.arbitration_id)
        if engine_info is None:
            logger.warning(
                "CAN arbitration id 0x%X does not map to any configured engine; dropping frame",
                msg.arbitration_id,
            )
            return

        mission_id = self.mission_id_resolver(msg)
        # Sequence counters are kept per-engine (not per raw arbitration_id
        # bucket) so out-of-order detection is correct even though a single
        # engine emits frames across several arbitration IDs (0x100-0x10F).
        seq = self._seq_counters.get(engine_info.engine_id, 0)
        self._seq_counters[engine_info.engine_id] = seq + 1

        for signal_name, value in decoded.items():
            sensor_type, unit = self._infer_sensor_type(signal_name)
            reading = NormalizedReading(
                mission_id=mission_id,
                drone_id=engine_info.drone_id,
                engine_id=engine_info.engine_id,
                drone_type=engine_info.drone_type,
                sensor_id=signal_name,
                sensor_type=sensor_type,
                value=float(value),
                unit=unit,
                timestamp=datetime.fromtimestamp(msg.timestamp, tz=timezone.utc),
                sequence_no=seq,
                source_protocol="can",
            )
            order_status = self.reorder.check(reading)
            if order_status == "late":
                logger.debug(
                    "Late CAN reading for %s on engine %s (seq=%s)",
                    signal_name, engine_info.engine_id, seq,
                )

            try:
                validate_reading(reading)
            except ValidationError as exc:
                logger.warning("Dropping invalid CAN reading: %s", exc.reason)
                continue

            await self.queue.put(reading)

    @staticmethod
    def _infer_sensor_type(signal_name: str) -> tuple[str, str]:
        """Maps a DBC signal name to (sensor_type, unit). In production this
        mapping is config-driven (loaded from the DBC's own unit metadata or
        a sidecar YAML); a simple lookup is shown here for clarity."""
        mapping = {
            "vib": ("vibration", "g"),
            "temp": ("temperature", "C"),
            "press": ("pressure", "bar"),
            "rpm": ("rpm", "rpm"),
            "curr": ("current", "A"),
        }
        for key, val in mapping.items():
            if key in signal_name.lower():
                return val
        return ("unknown", "")

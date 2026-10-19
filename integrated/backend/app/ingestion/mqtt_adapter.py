"""
MQTT adapter: subscribes to sensor topics over TLS, decompresses payloads
where flagged, validates, and forwards NormalizedReading objects to the
shared backpressure queue.

Expected payload (JSON, optionally compressed):
{
  "mission_id": "flight-2026-0912",
  "drone_id": "drone-01",
  "engine_id": "drone-01-eng1",
  "sensor_id": "eng1_temp_01",
  "sensor_type": "temperature",
  "value": 87.4,
  "unit": "C",
  "timestamp": "2026-09-06T10:15:03Z",
  "sequence_no": 4821,
  "compression": "zstd" | "zlib" | "none"
}

`engine_id` is cross-checked against config/drone_configs.yaml via the
DroneRegistry -- a payload claiming an engine_id that doesn't belong to the
declared drone_id (or doesn't exist at all) is rejected rather than trusted,
so a malformed/spoofed topic can't get engine data cross-associated.

Same status as app/ingestion/can_adapter.py: not started anywhere in
app/main.py, and not the project's default live-ingestion path -- see that
module's docstring for why (WebSocket ingestion via `/ws/ingest` is used
instead) and for what wiring this back in for a real MQTT broker would need.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

import paho.mqtt.client as mqtt
from pydantic import ValidationError as PydanticValidationError

from app.ingestion.base import (
    BackpressureQueue,
    NormalizedReading,
    ReorderBuffer,
    ValidationError,
    decompress_payload,
    validate_reading,
)
from app.mission.drone_registry import DroneRegistry

logger = logging.getLogger(__name__)


class MQTTAdapter:
    def __init__(
        self,
        queue: BackpressureQueue,
        broker_host: str,
        broker_port: int,
        topic: str,
        drone_registry: DroneRegistry,
        tls_ca_certs: str | None = None,
        client_id: str = "pm-ingestion",
    ):
        self.queue = queue
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.topic = topic
        self.reorder = ReorderBuffer()
        self.drone_registry = drone_registry
        self._loop: asyncio.AbstractEventLoop | None = None

        self.client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv5)
        if tls_ca_certs:
            self.client.tls_set(ca_certs=tls_ca_certs, tls_version=mqtt.ssl.PROTOCOL_TLS_CLIENT)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        logger.info("MQTT connected: %s", reason_code)
        client.subscribe(self.topic, qos=1)

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage):
        # Schedule the async handling on the running event loop from this
        # (paho-internal) network thread.
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._handle_message(msg), self._loop)

    async def _handle_message(self, msg: mqtt.MQTTMessage) -> None:
        try:
            envelope = json.loads(msg.payload)
        except json.JSONDecodeError:
            logger.warning("Non-JSON MQTT payload on %s, dropping", msg.topic)
            return

        compression = envelope.pop("compression", "none")
        try:
            if compression != "none" and "value_blob" in envelope:
                raw = decompress_payload(bytes.fromhex(envelope.pop("value_blob")), compression)
                envelope.update(json.loads(raw))

            drone_id = envelope["drone_id"]
            engine_id = envelope["engine_id"]
            engine_info = self.drone_registry.get_engine_info(engine_id)
            if engine_info is None or engine_info.drone_id != drone_id:
                logger.warning(
                    "MQTT payload on %s claims engine_id='%s' for drone_id='%s', "
                    "which doesn't match the configured topology -- dropping",
                    msg.topic, engine_id, drone_id,
                )
                return

            reading = NormalizedReading(
                mission_id=envelope["mission_id"],
                drone_id=drone_id,
                engine_id=engine_id,
                drone_type=engine_info.drone_type,
                sensor_id=envelope["sensor_id"],
                sensor_type=envelope["sensor_type"],
                value=float(envelope["value"]),
                unit=envelope["unit"],
                timestamp=datetime.fromisoformat(envelope["timestamp"].replace("Z", "+00:00")),
                sequence_no=envelope.get("sequence_no"),
                source_protocol="mqtt",
            )
        except (KeyError, PydanticValidationError, ValueError) as exc:
            logger.warning("Malformed MQTT reading on %s: %s", msg.topic, exc)
            return

        order_status = self.reorder.check(reading)
        if order_status == "late":
            logger.debug("Late MQTT reading for %s", reading.sensor_id)

        try:
            validate_reading(reading)
        except ValidationError as exc:
            logger.warning("Dropping invalid MQTT reading: %s", exc.reason)
            return

        await self.queue.put(reading)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.client.connect(self.broker_host, self.broker_port, keepalive=30)
        self.client.loop_start()
        try:
            while True:
                await asyncio.sleep(3600)  # keep task alive; loop_start runs in its own thread
        finally:
            self.client.loop_stop()
            self.client.disconnect()

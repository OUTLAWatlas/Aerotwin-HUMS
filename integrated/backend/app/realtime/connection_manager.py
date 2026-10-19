"""In-memory WebSocket connection registry for pushing live dashboard
frames, keyed by drone_id.

The dashboard opens one WebSocket per selected airframe
(`/ws/telemetry?drone_id=<id>&token=<jwt>`) and demultiplexes engine 0 vs
engine 1 (dual-engine drones) itself via the `engineId` field inside each
message -- see bridge.py. So the only fan-out this manager has to do is
"every client currently watching this drone_id".

Scaling note: this registry is per-process. Run N API replicas behind a
load balancer and a broadcast only reaches clients connected to *this*
replica. The project already takes a dependency on Redis for the
ingestion backpressure queue / alert dedup (see app/core/config.py
`redis_url`); swapping this for Redis pub/sub so a broadcast fans out to
every replica is the natural next step for a multi-replica deployment,
and is left as a TODO rather than guessed at here.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, drone_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections[drone_id].add(websocket)
        logger.info("WS connected for drone_id=%s (now %d client(s))",
                    drone_id, len(self._connections[drone_id]))

    async def disconnect(self, drone_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            conns = self._connections.get(drone_id)
            if conns is not None:
                conns.discard(websocket)
                if not conns:
                    self._connections.pop(drone_id, None)
        logger.info("WS disconnected for drone_id=%s", drone_id)

    async def broadcast(self, drone_id: str, frames: list[dict]) -> None:
        """Sends each frame, in order, to every client watching `drone_id`.
        Frames are the raw dashboard message envelopes -- see bridge.py."""
        if not frames:
            return
        async with self._lock:
            targets = list(self._connections.get(drone_id, ()))
        if not targets:
            return
        dead: list[WebSocket] = []
        for frame in frames:
            for ws in targets:
                try:
                    await ws.send_json(frame)
                except Exception:
                    # The connection's own receive loop (routes_ws.py) will
                    # observe the disconnect and call disconnect() -- we
                    # just skip it here rather than raising mid-broadcast.
                    dead.append(ws)
        if dead:
            async with self._lock:
                conns = self._connections.get(drone_id)
                if conns is not None:
                    for ws in dead:
                        conns.discard(ws)


# Single shared instance for the process -- imported by routes_ws.py (to
# register/unregister connections) and by whatever produces results
# (routes_predictions.py) to broadcast them.
connection_manager = ConnectionManager()

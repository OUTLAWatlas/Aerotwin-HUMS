"""
Streams synthetic sensor readings into `/ws/ingest` so you can exercise the
whole live pipeline -- ingestion -> orchestration -> alerting -> WebSocket
broadcast -> dashboard -- without any real CAN/MQTT hardware attached.

This is what replaces "plug in a CAN bus" for local development: run this
script in one terminal, open the dashboard in the browser, sign in, and pick
the airframe matching --drone-id (see AIRFRAME_TO_DRONE_ID in
frontend/src/config.js) to watch it update live.

Usage:
    pip install websockets   # not in requirements.txt yet -- add it, or
                              # `pip install websockets==13.1` directly
    python -m scripts.simulate_telemetry
    python -m scripts.simulate_telemetry --profile aerotwin --drone-id drone-01 --engine-id drone-01-eng1
    python -m scripts.simulate_telemetry --inject-fault-after 15

Two channel profiles, matching the two routing paths in
config/model_routing.yaml:
    generic   -- vibration/temperature/pressure (routes to the ML/physics-
                 residual models)
    aerotwin  -- all 13 AeroTwin channels (routes to the physics twin)

Nominal values for the "aerotwin" profile are illustrative only -- they
exercise the pipeline end-to-end but aren't calibrated against the real
reference trajectories in vendor/aerotwin_physics (that calibration lives
in a version-pinned calibration.json, not exposed as simple constants).
Good enough to see anomaly/fault/RUL frames flow through; not a substitute
for real test-rig or flight data.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import random
import sys

import httpx
import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("simulate_telemetry")

GENERIC_NOMINAL = {"vibration": 0.3, "temperature": 92.0, "pressure": 4.2}
GENERIC_UNITS = {"vibration": "g", "temperature": "C", "pressure": "bar"}

# Illustrative only -- see module docstring.
AEROTWIN_NOMINAL = {
    "rpm": 2400.0, "oil_pressure": 55.0, "map_kpa": 95.0, "fuel_flow": 18.0,
    "vibration": 0.3, "cht_a": 175.0, "cht_b": 175.0, "egt_a": 720.0,
    "egt_b": 720.0, "turbine_inlet_temp_a": 650.0, "turbine_inlet_temp_b": 650.0,
    "sensor_4": 0.5, "sensor_11": 0.5,
}

NOMINAL_OPERATING_POINT = {
    "altitude_m": 2200.0,
    "airspeed_ms": 140.0,
    "throttle_position": 0.55,
    "engine_load": 0.5,
    "propeller_pitch_deg": 30.0,
    "fuel_flow_command": 17.0,
    "ambient_temperature_c": 10.0,
}


def build_reading(profile: str, tick: int, fault_active: bool) -> dict:
    nominal = GENERIC_NOMINAL if profile == "generic" else AEROTWIN_NOMINAL
    units = GENERIC_UNITS if profile == "generic" else {}

    sensor_data = {}
    for channel, base in nominal.items():
        noise = base * random.uniform(-0.02, 0.02)
        value = base + noise
        if fault_active and channel in ("vibration", "cht_a", "egt_a"):
            # Ramp the affected channel well outside its nominal band so
            # anomaly detection actually fires -- this is what you'd see in
            # the dashboard's "Inject fault" demo, but coming from the real
            # backend pipeline instead of the frontend's own simulator.
            value *= 3.5
        sensor_data[channel] = {"value": round(value, 3), "unit": units.get(channel)}

    payload = {
        "mission_id": None,  # filled in by caller
        "drone_id": None,
        "engine_id": None,
        "sensor_data": sensor_data,
    }
    if profile == "aerotwin":
        payload["operating_point"] = dict(NOMINAL_OPERATING_POINT)
    return payload


async def login(http_base: str, username: str, password: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{http_base}/api/v1/auth/login",
            json={"user_id": username, "password": password},
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def run(args: argparse.Namespace) -> None:
    token = await login(args.http_base, args.username, args.password)
    logger.info("Logged in as %s", args.username)

    ws_url = f"{args.ws_base}/ws/ingest?token={token}"
    async with websockets.connect(ws_url) as ws:
        logger.info("Connected to %s", args.ws_base)
        for tick in itertools.count(1):
            if args.count and tick > args.count:
                break
            fault_active = bool(args.inject_fault_after) and tick >= args.inject_fault_after
            reading = build_reading(args.profile, tick, fault_active)
            reading["mission_id"] = args.mission_id
            reading["drone_id"] = args.drone_id
            reading["engine_id"] = args.engine_id

            await ws.send(json.dumps(reading))
            ack_raw = await ws.recv()
            ack = json.loads(ack_raw)
            status = "FAULT INJECTED" if fault_active else "nominal"
            if ack.get("type") == "error":
                logger.warning("tick %d [%s]: rejected -- %s", tick, status, ack.get("detail"))
            else:
                logger.info(
                    "tick %d [%s]: ok (rule=%s)", tick, status, ack.get("rule_used"),
                )
            await asyncio.sleep(args.interval)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--http-base", default="http://192.168.110.223:8000", help="Backend REST base URL")
    parser.add_argument("--ws-base", default="ws://192.168.110.223:8000", help="Backend WebSocket base URL")
    parser.add_argument("--username", default="pilot")
    parser.add_argument("--password", default="pilot123")
    parser.add_argument("--mission-id", default="sim-mission-01")
    parser.add_argument("--drone-id", default="drone-02")
    parser.add_argument("--engine-id", default="drone-02-eng1")
    parser.add_argument("--profile", choices=["generic", "aerotwin"], default="generic")
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between readings")
    parser.add_argument("--count", type=int, default=0, help="Number of readings to send (0 = run until Ctrl-C)")
    parser.add_argument(
        "--inject-fault-after", type=int, default=0,
        help="Start sending out-of-range values after this many ticks (0 = never)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info("Stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()

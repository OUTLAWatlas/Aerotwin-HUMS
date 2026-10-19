"""Translates backend domain objects (prediction results, alerts, mission
risk tiers) into the JSON frame contract documented in the dashboard's
`aerotwin-hums-dashboard.jsx` ("BACKEND INTEGRATION CONTRACT" section).

This module is the one place that knows about *both* sides' data shapes.
Everything here is best-effort where the two systems' domain models don't
line up 1:1 -- each of those spots is commented explicitly rather than
silently guessed at. In particular:

* The dashboard's telemetry panel is wired for a 4-cylinder piston engine
  (cht0..cht3 / egt0..egt3). AeroTwin only instruments an A/B sensor pair
  per engine (cht_a/cht_b, egt_a/egt_b) -- there's no per-cylinder
  granularity beyond that. We mirror A -> cylinders 1&2 and B -> cylinders
  3&4 so every dashboard field is populated, but this is a display
  approximation, not four independent sensors.
* Channels the dashboard expects that neither AeroTwin nor the generic
  sensor path produce today (batteryV, injectorTimingDeg) fall back to a
  fixed nominal value (NOMINAL_DEFAULTS below) so a telemetry frame is
  always complete -- the dashboard's `normalizeTelemetryPayload` indexes
  every field unconditionally and throws if one is missing.
* AeroTwin's fault taxonomy (misfire, ring_wear, cooling_restriction,
  oil_starvation, wastegate_leak) has no cylinder number attached. We
  guess cylinder 1 vs 3 from which of the A/B channels has the larger
  residual magnitude; treat `cylinder` in the resulting "diagnosis" frame
  as indicative, not a confirmed per-cylinder fault location.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.mission.drone_registry import DroneRegistry, EngineInfo

# --------------------------------------------------------------------------- #
# Engine index resolution
# --------------------------------------------------------------------------- #
# The dashboard identifies engines by a 0-based `engineId` (0 = first engine
# on the airframe; for TAPAS-BH-style dual-engine drones, 0 = Engine 1/Port,
# 1 = Engine 2/Starboard). drone_configs.yaml instead names engines by
# position ("left"/"right"/"center"), so map that to an index rather than
# hardcoding drone_id-specific logic here.
_POSITION_TO_INDEX = {"left": 0, "right": 1, "center": 0}


def engine_index(drone_registry: DroneRegistry, engine_id: str) -> int:
    info: Optional[EngineInfo] = drone_registry.get_engine_info(engine_id)
    if info is None:
        return 0
    if info.position in _POSITION_TO_INDEX:
        return _POSITION_TO_INDEX[info.position]
    # Fallback: ordinal position within the drone's engine list.
    engines = drone_registry.get_engines_for_drone(info.drone_id)
    return engines.index(engine_id) if engine_id in engines else 0


# --------------------------------------------------------------------------- #
# Telemetry
# --------------------------------------------------------------------------- #
# Nominal values used to fill any dashboard telemetry field this backend
# has no real source for yet, so a frame is always complete. Update these
# (or wire in a real sensor/model) as the two systems' channel sets converge.
NOMINAL_DEFAULTS: dict[str, float] = {
    "rpm": 2400.0,
    "oilPressureBar": 4.2,
    "oilTempC": 92.0,
    "fuelFlowLph": 18.0,
    "vibrationG": 0.3,
    "batteryV": 12.6,          # not modeled by either backend path yet
    "injectorTimingDeg": 12.0,  # not modeled by either backend path yet
    "ambientTempC": 15.0,
    "throttlePct": 65.0,
    "cht": 175.0,
    "egt": 720.0,
}

# AeroTwin channel name -> (dashboard field, scalar or cht/egt bank)
_AEROTWIN_SCALAR_MAP = {
    "rpm": "rpm",
    "oil_pressure": "oilPressureBar",
    "fuel_flow": "fuelFlowLph",
    "vibration": "vibrationG",
}
# cht_a/egt_a -> cylinders 1&2, cht_b/egt_b -> cylinders 3&4 (see module docstring)
_AEROTWIN_BANK_MAP = {
    "cht_a": ("cht", (0, 1)), "cht_b": ("cht", (2, 3)),
    "egt_a": ("egt", (0, 1)), "egt_b": ("egt", (2, 3)),
}

# Generic (non-AeroTwin) sensor path -- see PredictionRequest docstring:
# 'vibration', 'temperature', 'pressure'. There's no oil-specific channel
# in the generic path, so 'temperature'/'pressure' are treated as the
# closest engine-health-relevant analog (oil temp / oil pressure).
_GENERIC_SCALAR_MAP = {
    "vibration": "vibrationG",
    "temperature": "oilTempC",
    "pressure": "oilPressureBar",
}


def _pair(actual: float, expected: float) -> dict[str, float]:
    return {"actual": actual, "expected": expected}


def build_telemetry_frame(
    engine_idx: int,
    sensor_payload: dict[str, dict[str, Any]],
    residuals: Optional[dict[str, float]] = None,
    operating_point: Optional[dict[str, float]] = None,
) -> dict:
    """`sensor_payload` is the same dict sent to POST /api/v1/predict
    (`{channel: {"value": ..., "unit": ...}}`), i.e. the *actual* readings.
    `residuals` (AeroTwin-routed calls only) is `{channel: actual - expected}`
    from aerotwin_physics.service.serialise_output()["residuals"], used to
    recover a per-channel expected value: expected = actual - residual.
    """
    residuals = residuals or {}
    fields: dict[str, dict[str, float]] = {}
    cht = [None, None, None, None]
    egt = [None, None, None, None]

    for channel, reading in sensor_payload.items():
        try:
            actual = float(reading["value"] if isinstance(reading, dict) else reading)
        except (KeyError, TypeError, ValueError):
            continue

        if channel in _AEROTWIN_SCALAR_MAP:
            dash_field = _AEROTWIN_SCALAR_MAP[channel]
            expected = actual - residuals.get(channel, 0.0)
            fields[dash_field] = _pair(actual, expected)
        elif channel in _AEROTWIN_BANK_MAP:
            bank, indices = _AEROTWIN_BANK_MAP[channel]
            expected = actual - residuals.get(channel, 0.0)
            target = cht if bank == "cht" else egt
            for i in indices:
                target[i] = _pair(actual, expected)
        elif channel in _GENERIC_SCALAR_MAP:
            dash_field = _GENERIC_SCALAR_MAP[channel]
            # No baseline/expected model on the generic path -- report the
            # actual value with no injected deviation rather than fabricate one.
            fields.setdefault(dash_field, _pair(actual, actual))

    if operating_point:
        if "ambient_temperature_c" in operating_point:
            v = operating_point["ambient_temperature_c"]
            fields.setdefault("ambientTempC", _pair(v, v))
        if "throttle_position" in operating_point:
            v = operating_point["throttle_position"] * 100.0
            fields.setdefault("throttlePct", _pair(v, v))

    # Fill anything still missing with the nominal default so the frame is
    # always complete (see NOMINAL_DEFAULTS docstring above).
    for dash_field in (
        "rpm", "oilPressureBar", "oilTempC", "fuelFlowLph", "vibrationG",
        "batteryV", "injectorTimingDeg", "ambientTempC", "throttlePct",
    ):
        fields.setdefault(dash_field, _pair(NOMINAL_DEFAULTS[dash_field], NOMINAL_DEFAULTS[dash_field]))
    for i in range(4):
        if cht[i] is None:
            cht[i] = _pair(NOMINAL_DEFAULTS["cht"], NOMINAL_DEFAULTS["cht"])
        if egt[i] is None:
            egt[i] = _pair(NOMINAL_DEFAULTS["egt"], NOMINAL_DEFAULTS["egt"])

    payload = {
        **fields,
        "cht": {f"c{i+1}": cht[i] for i in range(4)},
        "egt": {f"c{i+1}": egt[i] for i in range(4)},
    }
    return {"type": "telemetry", "engineId": engine_idx, "payload": payload}


# --------------------------------------------------------------------------- #
# Health / anomaly / diagnosis / RUL
# --------------------------------------------------------------------------- #
def build_health_index_frame(engine_idx: int, health_score_0_1: float) -> dict:
    # DB/model layer uses a 0-1 fraction; the dashboard's health gauge is 0-100.
    value = max(0.0, min(100.0, health_score_0_1 * 100.0))
    return {"type": "health_index", "engineId": engine_idx, "payload": {"value": value}}


def build_anomaly_frame(engine_idx: int, detected: bool, timestamp_iso: Optional[str] = None) -> dict:
    payload = {"detected": detected}
    if detected and timestamp_iso:
        payload["timestamp"] = timestamp_iso
    return {"type": "anomaly", "engineId": engine_idx, "payload": payload}


def _guess_cylinder(fault_channel_hint: Optional[str]) -> int:
    """AeroTwin's fault taxonomy has no cylinder number (see module
    docstring) -- best-effort guess from which A/B channel is implicated."""
    if fault_channel_hint and fault_channel_hint.endswith("_b"):
        return 3
    return 1


def build_diagnosis_frame(
    engine_idx: int,
    fault_name: Optional[str],
    confidence_0_1: float,
    fault_channel_hint: Optional[str] = None,
) -> dict:
    if not fault_name or fault_name.upper() == "NO_FAULT" or fault_name == "none":
        return {"type": "diagnosis", "engineId": engine_idx, "payload": None}
    payload = {
        "name": fault_name,
        "cylinder": _guess_cylinder(fault_channel_hint),
        "confidencePct": round(confidence_0_1 * 100.0, 1),
    }
    return {"type": "diagnosis", "engineId": engine_idx, "payload": payload}


def build_rul_frame(
    engine_idx: int,
    median_hours: Optional[float],
    ci_lower_hours: Optional[float] = None,
    ci_upper_hours: Optional[float] = None,
) -> dict:
    if median_hours is None:
        return {"type": "rul", "engineId": engine_idx, "payload": None}
    # The dashboard wants a symmetric sigma, not a CI -- approximate it from
    # a 95% CI width (~ +/-1.96 sigma) when we have one; otherwise fall back
    # to a fixed fraction of the median as a rough uncertainty band.
    if ci_lower_hours is not None and ci_upper_hours is not None:
        sigma = max(0.0, (ci_upper_hours - ci_lower_hours) / (2 * 1.96))
    else:
        sigma = median_hours * 0.15
    return {
        "type": "rul", "engineId": engine_idx,
        "payload": {"medianHours": median_hours, "sigmaHours": sigma},
    }


# --------------------------------------------------------------------------- #
# Mission risk
# --------------------------------------------------------------------------- #
# Backend risk tiers (mission/manager.py) are low/medium/high/critical; the
# dashboard's risk banner only has three levels.
_RISK_TIER_TO_DASHBOARD_LEVEL = {
    "low": "nominal",
    "medium": "nominal",
    "high": "caution",
    "critical": "critical",
}

_RISK_MESSAGES = {
    "nominal": "Mission risk nominal.",
    "caution": "Elevated mission risk -- monitor engine health closely.",
    "critical": "Critical mission risk -- review engine health immediately.",
}


def build_mission_risk_frame(engine_idx: int, risk_tier: str, message: Optional[str] = None) -> dict:
    level = _RISK_TIER_TO_DASHBOARD_LEVEL.get(risk_tier, "nominal")
    return {
        "type": "mission_risk", "engineId": engine_idx,
        "payload": {"level": level, "message": message or _RISK_MESSAGES[level]},
    }


# --------------------------------------------------------------------------- #
# Log / reset
# --------------------------------------------------------------------------- #
_SEVERITY_TO_LOG_LEVEL = {"critical": "critical", "high": "critical", "medium": "warning", "low": "info"}


def build_log_frame(engine_idx: int, text: str, severity: Optional[str] = None) -> dict:
    level = _SEVERITY_TO_LOG_LEVEL.get(severity, "info")
    return {
        "type": "log", "engineId": engine_idx,
        "payload": {"text": text, "level": level, "time": datetime.now(timezone.utc).isoformat()},
    }


def build_reset_frame(engine_idx: int) -> dict:
    return {"type": "reset", "engineId": engine_idx, "payload": {}}

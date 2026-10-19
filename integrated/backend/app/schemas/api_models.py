from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    user_id: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class MeResponse(BaseModel):
    """Used by GET /api/v1/auth/me -- lets a client (e.g. the AeroTwin-HUMS
    dashboard) restore a session from a stored access token after a page
    refresh, without re-sending credentials."""
    user_id: str
    role: str


# --------------------------------------------------------------------------- #
# Prediction
# --------------------------------------------------------------------------- #
class SensorReadingIn(BaseModel):
    value: float
    unit: Optional[str] = None  # several AeroTwin channels have no calibrated unit


class PredictionRequest(BaseModel):
    mission_id: str
    drone_id: str
    engine_id: str
    sensor_data: dict[str, SensorReadingIn] = Field(
        ..., description=(
            "Keyed by sensor_type/channel name. For generic sensors: "
            "'vibration', 'temperature', 'pressure'. For AeroTwin-routed "
            "piston engines: all 13 channels in aerotwin_physics.config.CHANNELS "
            "(egt_a, egt_b, oil_pressure, sensor_4, rpm, map_kpa, fuel_flow, "
            "vibration, turbine_inlet_temp_a/b, sensor_11, cht_a, cht_b)."
        )
    )
    operating_point: Optional[dict[str, float]] = Field(
        default=None,
        description=(
            "Required only when routed to AeroTwin. Must include altitude_m, "
            "airspeed_ms, throttle_position, engine_load, propeller_pitch_deg, "
            "fuel_flow_command; ambient_pressure_pa/ambient_temperature_c are "
            "optional but preferred over AeroTwin's ISA fallback when available."
        ),
    )


class ModelCallResultOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_name: str
    model_version: str
    task: str
    output: dict
    confidence: float
    execution_time_ms: float
    used_fallback: bool
    degraded: bool


class PredictionResponse(BaseModel):
    mission_id: str
    drone_id: str
    engine_id: str
    drone_type: str
    rule_used: str
    results: list[ModelCallResultOut]
    total_execution_time_ms: float


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
class AlertOut(BaseModel):
    alert_id: str
    mission_id: str
    drone_id: str
    engine_id: str
    drone_type: str
    timestamp: str
    severity: str
    description: str
    recommendation: str
    confidence: float


# --------------------------------------------------------------------------- #
# Mission / config management
# --------------------------------------------------------------------------- #
class MissionCreateRequest(BaseModel):
    mission_id: str
    drone_id: str
    flight_duration_planned_min: Optional[int] = None
    operating_conditions: Optional[dict[str, bool]] = None
    risk_tolerance: str = Field(default="medium", pattern="^(low|medium|high)$")


class MissionOut(BaseModel):
    mission_id: str
    drone_id: str
    drone_type: str
    flight_duration_planned_min: Optional[int]
    operating_conditions: Optional[dict]
    risk_tolerance: str
    risk_score: Optional[float]
    status: str


class EngineHealthComparisonOut(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    engine_id: str
    health_score: Optional[float]
    as_of: Optional[str]
    model_version: Optional[str]


# --------------------------------------------------------------------------- #
# Ingestion (HTTP fallback path -- primary paths are CAN/MQTT adapters)
# --------------------------------------------------------------------------- #
class IngestReadingRequest(BaseModel):
    mission_id: str
    drone_id: str
    engine_id: str
    sensor_id: str
    sensor_type: str
    value: float
    unit: str
    timestamp: str
    sequence_no: Optional[int] = None

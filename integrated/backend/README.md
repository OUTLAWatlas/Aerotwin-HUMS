# Predictive Maintenance Inference Orchestration Platform (Multi-Drone / Multi-Engine)

A production-grade backend that ingests sensor data from CAN/MQTT sources across a
fleet of drones, routes it by **drone type and engine ID**, runs it through a
config-driven orchestration layer across ML and physics-based models, persists
results in TimescaleDB (partitioned by engine), raises per-engine deduplicated/
ranked alerts, and exposes everything through a secured FastAPI service.

## 0. Supported Drone/Engine Configurations

Three functional piston-engine configurations, declared in
`config/drone_configs.yaml` (the single source of truth -- nothing about
drone/engine topology is hardcoded in Python):

| drone_id  | drone_type      | engines                                   |
|-----------|-----------------|--------------------------------------------|
| drone-01  | `dual_engine`   | `drone-01-eng1` (left), `drone-01-eng2` (right) |
| drone-02  | `single_engine` | `drone-02-eng1` (center)                  |
| drone-03  | `single_engine` | `drone-03-eng1` (center)                  |

Every reading, model call, DB row, alert, and audit-log entry carries
`drone_id` + `engine_id` (+ derived `drone_type`) end to end, so a dual-engine
drone's two simultaneous data streams are never confused with each other or
with a different drone.

## 1. Architecture Overview

```
                     ┌─────────────────────────────────────────────────────────┐
                     │                     Clients / Dashboard                  │
                     └───────────────────────────┬───────────────────────────────┘
                                                  │ HTTPS (TLS 1.3) + JWT
                     ┌───────────────────────────▼───────────────────────────────┐
                     │                    FastAPI API Layer                      │
                     │  /predict  /ingest  /alerts  /missions  /auth  /health    │
                     │  - Pydantic validation                                    │
                     │  - JWT auth + RBAC dependency                             │
                     │  - Audit log middleware                                   │
                     └───────────────┬───────────────────────┬───────────────────┘
                                     │                        │
                 ┌───────────────────▼──────────┐   ┌─────────▼────────────────┐
                 │   Ingestion Layer             │   │  Mission Config Mgmt     │
                 │  CAN adapter / MQTT adapter   │   │  Risk assessment engine  │
                 │  validation, decompression,   │   │  Mission profile store   │
                 │  timestamp normalization,     │   └─────────┬────────────────┘
                 │  backpressure queue           │             │
                 └───────────────┬───────────────┘             │
                                 │ normalized readings          │
                     ┌───────────▼─────────────────────────────▼──────┐
                     │        Model Orchestration Layer (rules.yaml)   │
                     │  routes to ML / physics model based on:         │
                     │  sensor availability, mission risk, fallback    │
                     └───────────────┬─────────────────────────────────┘
                                     │
                     ┌───────────────▼─────────────────────────────────┐
                     │        Model Serving Layer (in-memory registry)  │
                     │  ML models (anomaly, fault, RUL)                │
                     │  Physics models (residual generators)           │
                     │  versioning, timeout, perf logging              │
                     └───────────────┬─────────────────────────────────┘
                                     │ results
                 ┌───────────────────▼───────────────┐   ┌────────────────────────┐
                 │     TimescaleDB (hypertables)      │   │   Alerting Service     │
                 │  readings, residuals, health_index, │◄──┤  dedup, severity rank, │
                 │  anomalies, faults, rul_predictions │   │  recommendations       │
                 │  audit_log                          │   └────────────────────────┘
                 └─────────────────────────────────────┘
```

## 2. Package Layout

```
predictive_maintenance/
  app/
    core/            # settings, security (JWT/RBAC), audit logging (+ drone/engine fields)
    ingestion/        # CAN + MQTT adapters (drone/engine-aware), validation, backpressure queue
    db/               # SQLAlchemy models, session, schema.sql
    orchestration/     # routing engine driven by config/model_routing.yaml (matches drone_type too)
    models_serving/    # model registry/loader, inference wrapper w/ timeout
    alerting/          # dedup (per-engine) + severity ranking + recommendation engine
    mission/           # mission profile CRUD + risk scoring + drone_registry.py (topology lookups)
                        # + multi-engine health comparison
    schemas/           # Pydantic request/response models (drone_id/engine_id on every model)
    api/               # FastAPI routers
    main.py            # app factory, startup model + drone-topology loading, TLS-ready uvicorn
  config/
    model_routing.yaml # orchestration decision table incl. drone_type matching
    drone_configs.yaml  # drone/engine topology: the 3 supported configurations
    roles.yaml          # RBAC permission matrix
  docker/
    Dockerfile
    docker-compose.yml  # api + timescaledb + mosquitto (MQTT broker)
  requirements.txt
```

## 3. Design Notes

- **Statelessness**: The API holds no session state; horizontal scaling is done by
  running N replicas behind a load balancer, all pointing at the same TimescaleDB
  instance and a shared Redis-backed backpressure queue (see `ingestion/base.py`).
- **Drone/engine topology as config, not code**: `config/drone_configs.yaml` is the
  single source of truth for which engines belong to which drone (and, for CAN,
  which arbitration-ID range belongs to which engine). `app/mission/drone_registry.py`
  loads it once and is used by ingestion, orchestration, and mission management --
  adding a 4th drone or changing an engine's CAN ID range is a config change, not a
  code change.
- **Config-driven orchestration**: `orchestration/router.py` never hardcodes
  "if drone_type == X call model Y". It loads `config/model_routing.yaml`, which
  maps sensor-availability + mission risk tier + **drone_type** -> ordered list of
  model calls with fallback rules (e.g. dual-engine drones route to a physics model
  calibrated for cross-engine vibration coupling). Ops can change routing without a
  code deploy.
- **No cross-engine collision**: CAN frames are mapped to an engine via
  arbitration-ID ranges (config-driven); MQTT payloads declare `drone_id` +
  `engine_id` and are rejected if the pairing doesn't match the registry. The
  out-of-order/reorder buffer and alert deduplication are both keyed by
  `(mission_id, engine_id, ...)`, so two engines emitting simultaneously never
  interleave or suppress each other's data/alerts.
- **Latency budget**: each model call runs under `asyncio.wait_for(..., timeout=0.1)`
  (100ms). On timeout/low confidence, the orchestrator falls back to the next model
  in the routing rule or returns a `degraded` result rather than blocking.
- **Traceability**: every table carries `mission_id`, `drone_id`, `engine_id`,
  `drone_type`, `model_version`, and `created_at`; the `sensor_readings` hypertable
  adds `engine_id` as a second (space) partitioning dimension so per-engine writes
  and scans stay fast and isolated; the `audit_log` table is a separate hypertable,
  also carrying `drone_id`/`engine_id`, so compliance data is never mixed with
  operational data.
- **Multi-engine correlation**: `GET /api/v1/missions/{mission_id}/engines/compare`
  returns the latest health index for every engine on that mission's drone side by
  side -- for a dual-engine drone this is how a dashboard spots one engine
  degrading relative to its twin.
- **Security**: TLS terminates at the reverse proxy/ingress in production (see
  docker-compose comments) with uvicorn also configured to serve TLS 1.3 directly
  for defense-in-depth; JWT access+refresh tokens; RBAC matrix in `roles.yaml`
  enforced via a FastAPI dependency; every request is audit-logged with drone/engine
  association when applicable.

## 3b. API Example: Prediction Request/Response

```http
POST /api/v1/predict
Authorization: Bearer <access_token>

{
  "mission_id": "flight-2026-0912",
  "drone_id": "drone-01",
  "engine_id": "drone-01-eng2",
  "sensor_data": {
    "vibration": {"value": 6.4, "unit": "g"},
    "temperature": {"value": 118.0, "unit": "C"},
    "pressure": {"value": 142.0, "unit": "bar"}
  }
}
```

```json
{
  "mission_id": "flight-2026-0912",
  "drone_id": "drone-01",
  "engine_id": "drone-01-eng2",
  "drone_type": "dual_engine",
  "rule_used": "dual_engine_high_risk",
  "results": [
    {
      "model_name": "physics_residual_dual_engine_v1",
      "model_version": "1.0.0-dual-engine",
      "task": "anomaly_detection",
      "output": {"anomaly_detected": true, "max_normalized_residual": 1.8},
      "confidence": 0.9,
      "execution_time_ms": 0.4,
      "used_fallback": false,
      "degraded": false
    }
  ],
  "total_execution_time_ms": 12.7
}
```

`engine_id` must belong to the given `drone_id` per `config/drone_configs.yaml`,
or the API returns `422`. Every downstream table row and derived alert carries
the same `drone_id` / `engine_id` / `drone_type` for full traceability.

## 4. Running Locally

```bash
cp .env.example .env
docker compose -f docker/docker-compose.yml up --build
# API docs: https://localhost:8443/docs
```

See inline docstrings in each module for further detail.

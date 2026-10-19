# AeroTwin-HUMS + Predictive Maintenance Orchestrator — Integrated

This folder combines the two projects you uploaded into one working
system:

- **`frontend/`** — the AeroTwin-HUMS React/Vite dashboard (was `HUMS-main`)
- **`backend/`** — the predictive-maintenance FastAPI service (was
  `predictive_maintenance_v5`)

The dashboard's `aerotwin-hums-dashboard.jsx` already documented a
"BACKEND INTEGRATION CONTRACT" (a WebSocket message format) that it was
built to receive from a real backend. The FastAPI service didn't have that
WebSocket endpoint, and the two projects' auth systems didn't agree on
request/response shapes or role names. This integration:

1. Points the dashboard's login at the FastAPI backend instead of the old
   Node/Express auth server, and reconciles the differences (see **Auth**).
2. Adds the WebSocket endpoint the dashboard was already designed to
   consume, and wires it into the existing prediction/alerting pipeline
   (see **Live telemetry bridge**).
3. Closes a gap in the backend itself: nothing previously called
   `AlertingService.build_alert()`, so no `Alert` rows were ever created.
   That's now wired in as part of the bridge.

Nothing in `backend/app/orchestration`, `app/alerting`, `app/mission`, or
the vendored AeroTwin physics engine was changed — the integration is a
thin adapter layer (`backend/app/realtime/`) plus a few call sites, on top
of the existing pipeline.

## Running it locally

You'll need **Python 3.11 or 3.12** (not 3.13/3.14 — `numpy==1.26.4` and
this repo's other pinned scientific packages don't ship prebuilt wheels for
newer versions yet, which means pip tries to compile from source and needs
a modern C toolchain most Windows machines don't have set up), Node 18+,
and a running Postgres/TimescaleDB instance.

**TimescaleDB via Docker is the easiest path**, including on Windows —
`docker/docker-compose.yml` already mounts `schema.sql` to auto-apply on
first startup, so there's no manual `psql` step:
```bash
cd backend
docker compose -f docker/docker-compose.yml up -d timescaledb
```
(You don't need the `redis`/`mosquitto` services for local dev — nothing
in `app/main.py`'s startup connects to them.)

```bash
# 1. Backend
cd backend
py -3.11 -m venv venv && venv\Scripts\activate   # Windows; use venv/bin/activate on macOS/Linux
cp .env.example .env               # edit PM_DATABASE_URL etc. as needed
pip install -r requirements.txt
uvicorn app.main:app --reload      # http://localhost:8000
# Demo users are auto-seeded on startup (see app/main.py's on_startup) --
# no separate seed step needed. Re-run `python -m scripts.seed_users`
# manually only if you want to reset their passwords.
```

```bash
# 2. Simulate live telemetry (separate terminal, optional but recommended --
# see "Live sensor ingestion" below for what this replaces)
pip install websockets==13.1
python -m scripts.simulate_telemetry
```

```bash
# 3. Frontend (separate terminal)
cd frontend
npm install
npm run dev                        # http://localhost:3000
```

Vite's dev server proxies both `/api/*` and `/ws/*` to `http://localhost:8000`
(see `frontend/vite.config.js`), so the browser only ever talks to
`localhost:3000` — no CORS configuration needed for local dev. Sign in with
any of the demo accounts and the dashboard will connect live.

### Demo accounts

Same usernames/passwords as the old Node server's `server/seed.js`, so
nothing to relearn:

| Username | Password    | Role     |
|----------|-------------|----------|
| admin    | admin123    | admin    |
| engineer | engineer123 | engineer |
| pilot    | pilot123    | pilot    |
| viewer   | viewer123   | viewer   |

### Running the frontend without a backend

Set `VITE_DISABLE_LIVE_BACKEND=true` in `frontend/.env` to force the
dashboard back into its original built-in simulator (the "Inject fault"
demo), even while signed in. Useful for demoing the UI without standing up
Postgres.

## Auth

| | Old Node server | FastAPI backend (now used) |
|---|---|---|
| Login body | `{ username, password }` | `{ user_id, password }` |
| Login response | `{ token, user }` | `{ access_token, refresh_token }` |
| Session restore | `GET /api/auth/me` | `GET /api/v1/auth/me` *(added)* |
| Token lifetime | 24h, no refresh | 15 min access / 7 day refresh |
| Roles | admin / engineer / pilot / viewer | admin / engineer / operator / viewer |

Changes made:

- **`backend/app/api/routes_auth.py`** — added `GET /me`, returning
  `{ user_id, role }` from the JWT, so the dashboard can restore a session
  after a page refresh the same way it always did.
- **`backend/config/roles.yaml`** — added a `pilot` role, identical to the
  existing `operator` role, so the dashboard's hardcoded role name
  (`src/auth/permissions.js`) resolves correctly against the backend's RBAC
  policy. `operator` is left in place for anything else that expects it.
- **`frontend/src/auth/AuthContext.jsx`** — rewritten to call the FastAPI
  endpoints with the right request/response shapes, store both tokens, and
  proactively refresh the access token every 10 minutes (below the
  backend's 15-minute expiry) so long-lived sessions — and the WebSocket
  connection below, which is authenticated with the same token — don't
  silently die mid-session.
- **`backend/scripts/seed_users.py`** *(new)* — creates the four demo users
  in Postgres with the same credentials the Node server used. Drones/engines
  don't need separate seeding; `app/db/schema.sql` already inserts them.

**Known simplification:** the WebSocket handshake carries the raw access
token as a `?token=` query parameter, because browsers can't attach an
`Authorization` header to a WebSocket upgrade. That's fine for local dev
but means the JWT can end up in proxy/access logs in production. A
production deployment should add a REST endpoint that exchanges a valid
access token for a short-lived, single-use WS ticket, and use that instead
— this is called out in `backend/app/api/routes_ws.py` but not implemented,
since it's a genuinely separate piece of work from the message contract
itself.

## Live telemetry bridge

New backend package: **`backend/app/realtime/`**

- `connection_manager.py` — an in-memory registry of open WebSocket
  connections, keyed by `drone_id`. Broadcasting is per-process; running
  multiple API replicas would need Redis pub/sub instead (the project
  already depends on Redis elsewhere) — flagged as a TODO in the module
  docstring rather than guessed at.
- `bridge.py` — turns backend results (prediction output, mission risk
  tier, alerts) into the exact JSON frames the dashboard's WebSocket client
  expects (`telemetry`, `health_index`, `anomaly`, `diagnosis`, `rul`,
  `mission_risk`, `log`). This is the file to read if you want to see
  exactly how the two systems' data shapes were reconciled — every
  approximation is commented in place, not hidden.

New endpoint: **`GET /ws/telemetry?drone_id=<id>&token=<jwt>`**
(`backend/app/api/routes_ws.py`), registered in `app/main.py`.

Hooked into: **`backend/app/api/routes_predictions.py`**'s `execute_prediction()`
— called from both `POST /api/v1/predict` and the new `/ws/ingest` endpoint
below, so every prediction, however it enters the system, in addition to
its existing behavior:

1. Builds a `telemetry` frame from the request's raw sensor readings.
2. Builds a `mission_risk` frame from the mission's current risk tier.
3. For each model result — anomaly, fault diagnosis, RUL, or AeroTwin's
   combined assessment — builds the matching `anomaly` / `diagnosis` /
   `rul` / `health_index` frame.
4. **Raises an `Alert` row** for any detected anomaly or fault, via
   `AlertingService.build_alert()` — previously unwired anywhere in the
   codebase — and, if it wasn't suppressed as a duplicate, broadcasts a
   `log` frame with the description and recommendation.
5. Broadcasts all of the above to whatever dashboard clients are watching
   that `drone_id`.

### Where the two systems' data models don't line up

This is the honest part. The dashboard was built around a generic
4-cylinder piston engine with channels like `batteryV` and
`injectorTimingDeg`; the backend's real model (AeroTwin) instruments a
different, narrower channel set. Rather than fabricate data quietly, every
approximation is a commented decision in `backend/app/realtime/bridge.py`:

- **Cylinder count**: AeroTwin only has an A/B sensor pair (`cht_a/cht_b`,
  `egt_a/egt_b`), not four independent cylinders. The bridge mirrors A into
  dashboard cylinders 1 & 2 and B into 3 & 4 — a display approximation, not
  four real sensors.
- **Missing channels**: `batteryV` and `injectorTimingDeg` aren't modeled
  by either backend path today. The bridge fills them with a fixed nominal
  value (`NOMINAL_DEFAULTS` in `bridge.py`) so the dashboard never crashes
  on a missing field — but they won't move in response to real telemetry
  until a real source is wired in.
- **"Expected" values**: for AeroTwin-routed channels, `expected` is
  recovered as `actual - residual` from AeroTwin's own residual output.
  For the generic (non-AeroTwin) sensor path there's no baseline model, so
  `expected` is just set equal to `actual` (no injected deviation) rather
  than invented.
- **Fault → cylinder number**: AeroTwin's fault taxonomy (`misfire`,
  `ring_wear`, `cooling_restriction`, `oil_starvation`, `wastegate_leak`)
  has no per-cylinder location. The bridge guesses cylinder 1 vs 3 from
  whichever of the A/B channels is implicated — treat it as indicative,
  not a confirmed fault location.
- **Risk tiers**: the backend has four risk tiers (low/medium/high/critical);
  the dashboard's risk banner only has three (nominal/caution/critical).
  Mapped low+medium → nominal, high → caution, critical → critical.

None of this blocks the integration from working end to end — it's the
kind of mismatch you'd expect from two independently-built systems, made
explicit instead of papered over.

## Live sensor ingestion: WebSocket, not CAN

The backend originally planned CAN bus (`app/ingestion/can_adapter.py`) as
the live-ingestion path, with MQTT (`app/ingestion/mqtt_adapter.py`) as an
alternative. Neither was ever actually started anywhere in `app/main.py`,
and CAN specifically can't run in local dev at all: `python-can`'s
`socketcan` backend is Linux-only and needs a real CAN interface attached.

Both adapters are left in the repo, unwired, for a future deployment that
does have real hardware — but the project's actual live-ingestion path now
is:

**`GET/Upgrade /ws/ingest?token=<jwt>`** (`app/api/routes_ws.py`) — a
telemetry source (a real engine's companion computer, or the simulator
script below) opens one persistent connection and sends a JSON message per
reading tick, shaped exactly like `POST /api/v1/predict`'s body
(`mission_id`, `drone_id`, `engine_id`, `sensor_data`, optional
`operating_point`). Each message runs through the *exact same* pipeline as
the REST endpoint — orchestration, persistence, alerting, and a broadcast
to `/ws/telemetry` clients — via a shared `execute_prediction()` function
(refactored out of `routes_predictions.py` for this). The server acks or
errors each message individually; a bad message doesn't close the
connection.

This endpoint requires `ingestion:write`, not `predictions:write` — it
models a telemetry-emitting device/operator (pilot, operator, engineer),
not a human directly invoking arbitrary predictions.

**Testing it without any real hardware:**
```bash
pip install websockets==13.1   # added to requirements.txt
python -m scripts.simulate_telemetry
```
This logs in as the `pilot` demo user, connects to `/ws/ingest`, and
streams synthetic readings for `drone-02` every 2 seconds. Open the
dashboard, sign in, select the matching airframe (see
`AIRFRAME_TO_DRONE_ID` in `frontend/src/config.js` — `drone-02` is
`uav12`), and watch it update live. Useful flags:

```bash
python -m scripts.simulate_telemetry --profile aerotwin --drone-id drone-01 --engine-id drone-01-eng1
python -m scripts.simulate_telemetry --inject-fault-after 15   # ramps values out of range to trigger an alert
```

Note: the old HTTP fallback (`POST /api/v1/ingest`) still exists but was
already storage-only (writes a `SensorReading` row, no orchestration) —
that hasn't changed and isn't what you want for live telemetry.

## CORS

`backend/app/main.py` now adds `CORSMiddleware`, allowing
`http://localhost:3000` (configurable via `PM_CORS_ORIGINS` /
`Settings.cors_origins` in `backend/app/core/config.py`). Not required for
the WebSocket upgrade itself, only for the plain REST calls.

## What wasn't changed

- The old Node/Express server (`frontend/server/`) is left in place but is
  no longer used by the frontend — it's superseded by the FastAPI backend's
  auth endpoints. Nothing deletes it in case you want it as a reference.
- The **Mission Replay**, **Mission Risk Assessment**, and **Predictive
  Maintenance** dashboard views still use the dashboard's own local demo
  data/logic for anything beyond what the live WebSocket feed drives (the
  Live Mission Control view). Wiring those to `GET /api/v1/missions/*` and
  `GET /api/v1/alerts` directly (rather than through the WS bridge) is a
  reasonable next step but is a separate, additive piece of work from the
  live-telemetry bridge built here.
- Production concerns flagged but not implemented: the WS ticket exchange
  (see **Auth** above), multi-replica broadcast via Redis pub/sub (see
  **Live telemetry bridge** above), and TLS (both projects already have
  their own TLS setup notes; nothing here changes that).

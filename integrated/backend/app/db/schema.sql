-- =====================================================================
-- Predictive Maintenance Platform -- TimescaleDB schema
-- Run against a PostgreSQL instance with the timescaledb extension.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------------------
-- Drone configuration and engine metadata (mirrors config/drone_configs.yaml)
-- ---------------------------------------------------------------------
CREATE TABLE drones (
    drone_id        TEXT PRIMARY KEY,
    drone_type      TEXT NOT NULL CHECK (drone_type IN ('dual_engine', 'single_engine')),
    engine_count    INTEGER NOT NULL CHECK (engine_count IN (1, 2)),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE engines (
    engine_id       TEXT PRIMARY KEY,
    drone_id        TEXT NOT NULL REFERENCES drones(drone_id),
    engine_type     TEXT NOT NULL DEFAULT 'piston',
    position        TEXT,                       -- 'left' | 'right' | 'center'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_engines_drone ON engines (drone_id);

-- ---------------------------------------------------------------------
-- Raw sensor readings (time-series hypertable), partitioned/indexed by
-- drone_type + engine_id so per-engine and per-drone-type queries are fast
-- and simultaneous multi-engine ingestion never collides.
-- ---------------------------------------------------------------------
CREATE TABLE sensor_readings (
    id              UUID DEFAULT uuid_generate_v4(),
    mission_id      TEXT        NOT NULL,
    drone_id        TEXT        NOT NULL,
    engine_id       TEXT        NOT NULL,
    drone_type      TEXT        NOT NULL,
    sensor_id       TEXT        NOT NULL,
    sensor_type     TEXT        NOT NULL,
    value           DOUBLE PRECISION NOT NULL,
    unit            TEXT        NOT NULL,
    source_protocol TEXT        NOT NULL,       -- 'can' | 'mqtt' | 'http'
    sequence_no     BIGINT,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    time            TIMESTAMPTZ NOT NULL,        -- normalized sensor timestamp
    -- engine_id must be part of the primary key: TimescaleDB requires every
    -- unique constraint on a hypertable to include all of its partitioning
    -- columns (time here, plus engine_id once declared as a space dimension
    -- below via add_dimension). PRIMARY KEY (time, id) alone makes that
    -- add_dimension call fail.
    PRIMARY KEY (time, engine_id, id)
);
SELECT create_hypertable('sensor_readings', 'time', chunk_time_interval => INTERVAL '1 day');
-- Space partitioning by engine_id gives each engine its own hypertable chunk
-- dimension, so concurrent writes from different engines never contend on
-- the same chunk and per-engine scans stay narrow.
SELECT add_dimension('sensor_readings', 'engine_id', number_partitions => 8);
CREATE INDEX idx_readings_engine_time ON sensor_readings (engine_id, time DESC);
CREATE INDEX idx_readings_drone_type_time ON sensor_readings (drone_type, time DESC);
CREATE INDEX idx_readings_mission_engine_sensor_time
    ON sensor_readings (mission_id, engine_id, sensor_id, time DESC);

-- ---------------------------------------------------------------------
-- Physics-model residuals (per engine)
-- ---------------------------------------------------------------------
CREATE TABLE residuals (
    id              UUID DEFAULT uuid_generate_v4(),
    mission_id      TEXT        NOT NULL,
    drone_id        TEXT        NOT NULL,
    engine_id       TEXT        NOT NULL,
    drone_type      TEXT        NOT NULL,
    sensor_id       TEXT        NOT NULL,
    physics_model   TEXT        NOT NULL,
    model_version   TEXT        NOT NULL,
    residual_value  DOUBLE PRECISION NOT NULL,
    threshold       DOUBLE PRECISION,
    time            TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (time, id)
);
SELECT create_hypertable('residuals', 'time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX idx_residuals_engine_time ON residuals (engine_id, time DESC);
CREATE INDEX idx_residuals_mission_time ON residuals (mission_id, time DESC);

-- ---------------------------------------------------------------------
-- Health index values over time (per engine)
-- ---------------------------------------------------------------------
CREATE TABLE health_index (
    id              UUID DEFAULT uuid_generate_v4(),
    mission_id      TEXT        NOT NULL,
    drone_id        TEXT        NOT NULL,
    engine_id       TEXT        NOT NULL,
    drone_type      TEXT        NOT NULL,
    component       TEXT        NOT NULL,
    health_score    DOUBLE PRECISION NOT NULL CHECK (health_score BETWEEN 0 AND 1),
    model_version   TEXT        NOT NULL,
    time            TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (time, id)
);
SELECT create_hypertable('health_index', 'time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX idx_health_engine_time ON health_index (engine_id, time DESC);
CREATE INDEX idx_health_drone_time ON health_index (drone_id, time DESC);

-- ---------------------------------------------------------------------
-- Anomaly flags (per engine)
-- ---------------------------------------------------------------------
CREATE TABLE anomalies (
    id                UUID DEFAULT uuid_generate_v4(),
    mission_id        TEXT        NOT NULL,
    drone_id          TEXT        NOT NULL,
    engine_id         TEXT        NOT NULL,
    drone_type        TEXT        NOT NULL,
    sensor_id         TEXT        NOT NULL,
    anomaly_type      TEXT        NOT NULL,
    severity          TEXT        NOT NULL,      -- critical|high|medium|low
    confidence        DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    model_name        TEXT        NOT NULL,
    model_version     TEXT        NOT NULL,
    detected_at       TIMESTAMPTZ NOT NULL,
    time              TIMESTAMPTZ NOT NULL,       -- partitioning column (= detected_at)
    PRIMARY KEY (time, id)
);
SELECT create_hypertable('anomalies', 'time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX idx_anomalies_engine_time ON anomalies (engine_id, time DESC);
CREATE INDEX idx_anomalies_mission_time ON anomalies (mission_id, time DESC);
CREATE INDEX idx_anomalies_severity ON anomalies (severity, time DESC);

-- ---------------------------------------------------------------------
-- Fault diagnoses (per engine)
-- ---------------------------------------------------------------------
CREATE TABLE fault_diagnoses (
    id              UUID DEFAULT uuid_generate_v4(),
    mission_id      TEXT        NOT NULL,
    drone_id        TEXT        NOT NULL,
    engine_id       TEXT        NOT NULL,
    drone_type      TEXT        NOT NULL,
    component       TEXT        NOT NULL,
    fault_code      TEXT        NOT NULL,
    description     TEXT,
    confidence      DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    model_name      TEXT        NOT NULL,
    model_version   TEXT        NOT NULL,
    time            TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (time, id)
);
SELECT create_hypertable('fault_diagnoses', 'time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX idx_faults_engine_time ON fault_diagnoses (engine_id, time DESC);
CREATE INDEX idx_faults_mission_time ON fault_diagnoses (mission_id, time DESC);

-- ---------------------------------------------------------------------
-- RUL (remaining useful life) predictions with confidence intervals (per engine)
-- ---------------------------------------------------------------------
CREATE TABLE rul_predictions (
    id                UUID DEFAULT uuid_generate_v4(),
    mission_id        TEXT        NOT NULL,
    drone_id          TEXT        NOT NULL,
    engine_id         TEXT        NOT NULL,
    drone_type        TEXT        NOT NULL,
    component         TEXT        NOT NULL,
    rul_value_hours   DOUBLE PRECISION NOT NULL,
    ci_lower_hours    DOUBLE PRECISION,
    ci_upper_hours    DOUBLE PRECISION,
    confidence_level  DOUBLE PRECISION DEFAULT 0.90,
    model_name        TEXT        NOT NULL,
    model_version     TEXT        NOT NULL,
    time              TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (time, id)
);
SELECT create_hypertable('rul_predictions', 'time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX idx_rul_engine_time ON rul_predictions (engine_id, time DESC);
CREATE INDEX idx_rul_mission_time ON rul_predictions (mission_id, time DESC);

-- ---------------------------------------------------------------------
-- Alerts (derived from anomalies / fault_diagnoses by the alerting service)
-- ---------------------------------------------------------------------
CREATE TABLE alerts (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    mission_id        TEXT        NOT NULL,
    drone_id          TEXT        NOT NULL,
    engine_id         TEXT        NOT NULL,
    drone_type        TEXT        NOT NULL,
    source_type       TEXT        NOT NULL,   -- 'anomaly' | 'fault'
    source_id         UUID        NOT NULL,
    severity          TEXT        NOT NULL,
    description       TEXT        NOT NULL,
    recommendation    TEXT        NOT NULL,
    confidence        DOUBLE PRECISION NOT NULL,
    dedup_key         TEXT        NOT NULL,     -- scoped per-engine, see alerting/service.py
    status            TEXT        NOT NULL DEFAULT 'open',  -- open|acknowledged|resolved
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_by   TEXT,
    acknowledged_at   TIMESTAMPTZ
);
CREATE INDEX idx_alerts_dedup ON alerts (dedup_key, created_at DESC);
CREATE INDEX idx_alerts_engine_status ON alerts (engine_id, status);
CREATE INDEX idx_alerts_mission_status ON alerts (mission_id, status);

-- ---------------------------------------------------------------------
-- Mission / flight configuration (tied to a drone + its engine configuration)
-- ---------------------------------------------------------------------
CREATE TABLE missions (
    mission_id        TEXT PRIMARY KEY,
    drone_id          TEXT        NOT NULL REFERENCES drones(drone_id),
    drone_type        TEXT        NOT NULL,
    flight_duration_planned_min INTEGER,
    operating_conditions JSONB,
    risk_tolerance    TEXT        NOT NULL DEFAULT 'medium',
    risk_score        DOUBLE PRECISION,
    status            TEXT        NOT NULL DEFAULT 'active',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_missions_drone ON missions (drone_id);

-- ---------------------------------------------------------------------
-- Users (for JWT auth demo -- production would federate to IdP)
-- ---------------------------------------------------------------------
CREATE TABLE users (
    user_id         TEXT PRIMARY KEY,
    hashed_password TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin','engineer','operator','pilot','viewer')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Audit log (separate hypertable for compliance/traceability)
-- ---------------------------------------------------------------------
CREATE TABLE audit_log (
    id            UUID DEFAULT uuid_generate_v4(),
    user_id       TEXT        NOT NULL,
    action        TEXT        NOT NULL,     -- HTTP method or explicit action name
    resource      TEXT        NOT NULL,     -- path or resource identifier
    drone_id      TEXT,                     -- nullable: not every call is engine-scoped
    engine_id     TEXT,
    outcome       TEXT        NOT NULL,     -- success|failure|error
    status_code   INTEGER,
    duration_ms   DOUBLE PRECISION,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    time          TIMESTAMPTZ NOT NULL,      -- = created_at, partitioning column
    PRIMARY KEY (time, id)
);
SELECT create_hypertable('audit_log', 'time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX idx_audit_user_time ON audit_log (user_id, time DESC);
CREATE INDEX idx_audit_engine_time ON audit_log (engine_id, time DESC);

-- ---------------------------------------------------------------------
-- Seed data mirroring config/drone_configs.yaml (3 supported configurations:
-- one dual-piston-engine drone, two single-piston-engine drones)
-- ---------------------------------------------------------------------
INSERT INTO drones (drone_id, drone_type, engine_count) VALUES
    ('drone-01', 'dual_engine', 2),
    ('drone-02', 'single_engine', 1),
    ('drone-03', 'single_engine', 1);

INSERT INTO engines (engine_id, drone_id, engine_type, position) VALUES
    ('drone-01-eng1', 'drone-01', 'piston', 'left'),
    ('drone-01-eng2', 'drone-01', 'piston', 'right'),
    ('drone-02-eng1', 'drone-02', 'piston', 'center'),
    ('drone-03-eng1', 'drone-03', 'piston', 'center');

-- ---------------------------------------------------------------------
-- Retention / compression policies (example -- tune per compliance needs)
-- ---------------------------------------------------------------------
ALTER TABLE sensor_readings SET (timescaledb.compress, timescaledb.compress_segmentby = 'engine_id, sensor_id');
SELECT add_compression_policy('sensor_readings', INTERVAL '7 days');
SELECT add_retention_policy('sensor_readings', INTERVAL '365 days');
SELECT add_retention_policy('audit_log', INTERVAL '2555 days'); -- 7-year compliance retention

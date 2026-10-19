"""
SQLAlchemy models mirroring db/schema.sql. Hypertable creation, the extra
space-partitioning dimension on sensor_readings, and compression/retention
policies are applied via raw SQL (schema.sql / Alembic migration), not
through the ORM.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint, Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Drone(Base):
    __tablename__ = "drones"
    drone_id = Column(String, primary_key=True)
    drone_type = Column(String, nullable=False)  # 'dual_engine' | 'single_engine'
    engine_count = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (
        CheckConstraint("drone_type IN ('dual_engine','single_engine')"),
    )


class Engine(Base):
    __tablename__ = "engines"
    engine_id = Column(String, primary_key=True)
    drone_id = Column(String, ForeignKey("drones.drone_id"), nullable=False, index=True)
    engine_type = Column(String, default="piston")
    position = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class SensorReading(Base):
    __tablename__ = "sensor_readings"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mission_id = Column(String, nullable=False, index=True)
    drone_id = Column(String, nullable=False, index=True)
    engine_id = Column(String, nullable=False, index=True, primary_key=True)
    drone_type = Column(String, nullable=False, index=True)
    sensor_id = Column(String, nullable=False)
    sensor_type = Column(String, nullable=False)
    value = Column(Float, nullable=False)
    unit = Column(String, nullable=False)
    source_protocol = Column(String, nullable=False)
    sequence_no = Column(Integer, nullable=True)
    ingested_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    time = Column(DateTime(timezone=True), primary_key=True, nullable=False)


class Residual(Base):
    __tablename__ = "residuals"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mission_id = Column(String, nullable=False, index=True)
    drone_id = Column(String, nullable=False)
    engine_id = Column(String, nullable=False, index=True)
    drone_type = Column(String, nullable=False)
    sensor_id = Column(String, nullable=False)
    physics_model = Column(String, nullable=False)
    model_version = Column(String, nullable=False)
    residual_value = Column(Float, nullable=False)
    threshold = Column(Float, nullable=True)
    time = Column(DateTime(timezone=True), primary_key=True, nullable=False)


class HealthIndex(Base):
    __tablename__ = "health_index"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mission_id = Column(String, nullable=False, index=True)
    drone_id = Column(String, nullable=False, index=True)
    engine_id = Column(String, nullable=False, index=True)
    drone_type = Column(String, nullable=False)
    component = Column(String, nullable=False)
    health_score = Column(Float, nullable=False)
    model_version = Column(String, nullable=False)
    time = Column(DateTime(timezone=True), primary_key=True, nullable=False)


class Anomaly(Base):
    __tablename__ = "anomalies"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mission_id = Column(String, nullable=False, index=True)
    drone_id = Column(String, nullable=False)
    engine_id = Column(String, nullable=False, index=True)
    drone_type = Column(String, nullable=False)
    sensor_id = Column(String, nullable=False)
    anomaly_type = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    confidence = Column(Float, nullable=False)
    model_name = Column(String, nullable=False)
    model_version = Column(String, nullable=False)
    detected_at = Column(DateTime(timezone=True), nullable=False)
    time = Column(DateTime(timezone=True), primary_key=True, nullable=False)


class FaultDiagnosis(Base):
    __tablename__ = "fault_diagnoses"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mission_id = Column(String, nullable=False, index=True)
    drone_id = Column(String, nullable=False)
    engine_id = Column(String, nullable=False, index=True)
    drone_type = Column(String, nullable=False)
    component = Column(String, nullable=False)
    fault_code = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    confidence = Column(Float, nullable=False)
    model_name = Column(String, nullable=False)
    model_version = Column(String, nullable=False)
    time = Column(DateTime(timezone=True), primary_key=True, nullable=False)


class RULPrediction(Base):
    __tablename__ = "rul_predictions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mission_id = Column(String, nullable=False, index=True)
    drone_id = Column(String, nullable=False)
    engine_id = Column(String, nullable=False, index=True)
    drone_type = Column(String, nullable=False)
    component = Column(String, nullable=False)
    rul_value_hours = Column(Float, nullable=False)
    ci_lower_hours = Column(Float, nullable=True)
    ci_upper_hours = Column(Float, nullable=True)
    confidence_level = Column(Float, default=0.90)
    model_name = Column(String, nullable=False)
    model_version = Column(String, nullable=False)
    time = Column(DateTime(timezone=True), primary_key=True, nullable=False)


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mission_id = Column(String, nullable=False, index=True)
    drone_id = Column(String, nullable=False)
    engine_id = Column(String, nullable=False, index=True)
    drone_type = Column(String, nullable=False)
    source_type = Column(String, nullable=False)
    source_id = Column(UUID(as_uuid=True), nullable=False)
    severity = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    recommendation = Column(Text, nullable=False)
    confidence = Column(Float, nullable=False)
    dedup_key = Column(String, nullable=False, index=True)
    status = Column(String, default="open")
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    acknowledged_by = Column(String, nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)


class Mission(Base):
    __tablename__ = "missions"
    mission_id = Column(String, primary_key=True)
    drone_id = Column(String, ForeignKey("drones.drone_id"), nullable=False, index=True)
    drone_type = Column(String, nullable=False)
    flight_duration_planned_min = Column(Integer, nullable=True)
    operating_conditions = Column(JSON, nullable=True)
    risk_tolerance = Column(String, default="medium")
    risk_score = Column(Float, nullable=True)
    status = Column(String, default="active")
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)


class User(Base):
    __tablename__ = "users"
    user_id = Column(String, primary_key=True)
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False)
    resource = Column(String, nullable=False)
    drone_id = Column(String, nullable=True)
    engine_id = Column(String, nullable=True, index=True)
    outcome = Column(String, nullable=False)
    status_code = Column(Integer, nullable=True)
    duration_ms = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    time = Column(DateTime(timezone=True), primary_key=True, nullable=False, default=datetime.utcnow)

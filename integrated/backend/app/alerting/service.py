"""
Converts anomaly/fault model outputs into deduplicated, severity-ranked,
recommendation-enriched alert payloads ready for frontend consumption.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.config import get_settings

settings = get_settings()

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Config-driven recommendation templates keyed by (source_type, severity).
# Production would load this from YAML/DB alongside model_routing.yaml.
RECOMMENDATIONS = {
    ("fault", "critical"): "Ground aircraft / stop operation immediately and inspect {component}.",
    ("fault", "high"): "Schedule maintenance within 48 hours for {component}.",
    ("fault", "medium"): "Schedule inspection of {component} at next maintenance window.",
    ("fault", "low"): "Monitor {component}; no immediate action required.",
    ("anomaly", "critical"): "Immediate investigation required on {component}.",
    ("anomaly", "high"): "Prioritize inspection of {component} within 24 hours.",
    ("anomaly", "medium"): "Add {component} to next scheduled check.",
    ("anomaly", "low"): "Continue monitoring {component}; log for trend analysis.",
}


def _severity_from_confidence(confidence: float, base_severity: str | None = None) -> str:
    if base_severity:
        return base_severity
    if confidence >= 0.9:
        return "critical"
    if confidence >= 0.7:
        return "high"
    if confidence >= 0.4:
        return "medium"
    return "low"


@dataclass
class AlertCandidate:
    mission_id: str
    drone_id: str
    engine_id: str
    drone_type: str
    source_type: str  # "anomaly" | "fault"
    source_id: str
    component: str
    confidence: float
    description: str
    severity: str | None = None


@dataclass
class AlertPayload:
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
    dedup_key: str


class AlertDeduplicator:
    """In-memory dedup store keyed by (mission_id, source_type, component).
    In a multi-replica deployment this is backed by Redis (same TTL
    semantics) so all API instances share dedup state."""

    def __init__(self, window_seconds: int):
        self.window_seconds = window_seconds
        self._last_seen: dict[str, float] = {}

    def is_duplicate(self, dedup_key: str) -> bool:
        now = time.monotonic()
        last = self._last_seen.get(dedup_key)
        if last is not None and (now - last) < self.window_seconds:
            return True
        self._last_seen[dedup_key] = now
        return False


class AlertingService:
    def __init__(self, dedup_window_seconds: int | None = None):
        self.deduplicator = AlertDeduplicator(
            dedup_window_seconds or settings.alert_dedup_window_seconds
        )

    def build_alert(self, candidate: AlertCandidate) -> AlertPayload | None:
        severity = _severity_from_confidence(candidate.confidence, candidate.severity)
        dedup_key = self._dedup_key(candidate)

        if self.deduplicator.is_duplicate(dedup_key):
            return None  # suppressed

        recommendation_template = RECOMMENDATIONS.get(
            (candidate.source_type, severity), "Review {component} at next opportunity."
        )
        recommendation = recommendation_template.format(component=candidate.component)

        import uuid

        return AlertPayload(
            alert_id=str(uuid.uuid4()),
            mission_id=candidate.mission_id,
            drone_id=candidate.drone_id,
            engine_id=candidate.engine_id,
            drone_type=candidate.drone_type,
            timestamp=datetime.now(timezone.utc).isoformat(),
            severity=severity,
            description=candidate.description,
            recommendation=recommendation,
            confidence=candidate.confidence,
            dedup_key=dedup_key,
        )

    @staticmethod
    def _dedup_key(candidate: AlertCandidate) -> str:
        # Scoped per engine_id: identical anomalies on two different engines
        # of the same dual-engine drone must NOT suppress each other.
        raw = f"{candidate.mission_id}:{candidate.engine_id}:{candidate.source_type}:{candidate.component}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    @staticmethod
    def rank(alerts: list[AlertPayload]) -> list[AlertPayload]:
        return sorted(alerts, key=lambda a: (SEVERITY_ORDER.get(a.severity, 99), a.timestamp))


# Process-wide singleton so alert dedup state (in-memory `AlertDeduplicator`)
# is shared between whoever raises candidates (routes_predictions.py, on
# every anomaly/fault) and whoever reads/acks the resulting rows
# (routes_alerts.py) -- mirrors the model_registry / connection_manager
# singleton pattern used elsewhere in this codebase.
alerting_service = AlertingService()

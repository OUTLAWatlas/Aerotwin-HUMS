"""
Centralized application settings, loaded from environment variables / .env.
Nothing here is hardcoded business logic -- routing/roles live in YAML.
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = BASE_DIR / "config"
MODELS_DIR = BASE_DIR / "models"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="PM_", extra="ignore", protected_namespaces=()
    )

    # --- App ---
    app_name: str = "predictive-maintenance-orchestrator"
    environment: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"

    # --- Database (TimescaleDB / Postgres) ---
    database_url: str = Field(
        default="postgresql+asyncpg://pm_user:pm_pass@localhost:5432/pm_db"
    )
    db_pool_size: int = 20
    db_max_overflow: int = 10

    # --- Security ---
    jwt_secret_key: str = Field(default="CHANGE_ME_IN_PROD")
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7
    tls_cert_path: str = "/etc/pm/certs/server.crt"
    tls_key_path: str = "/etc/pm/certs/server.key"
    tls_min_version: str = "TLSv1.3"

    # --- CORS (dashboard integration) ---
    # Origins allowed to call the REST API from a browser. The WebSocket
    # bridge (app/api/routes_ws.py) doesn't need this -- browsers don't
    # apply CORS preflight to the WS handshake -- but it's listed here too
    # for documentation/consistency with the frontend's dev server origin.
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000", "http://127.0.0.1:3000",
            "http://192.168.110.223:3000", "http://192.168.110.223:8000",
        ]
    )

    # --- MQTT / CAN ingestion ---
    mqtt_broker_host: str = "localhost"
    mqtt_broker_port: int = 8883  # TLS port
    mqtt_topic_prefix: str = "pm/sensors/#"
    can_interface: str = "can0"
    can_bustype: str = "socketcan"
    ingestion_queue_maxsize: int = 10_000

    # --- Model orchestration ---
    model_routing_config_path: Path = CONFIG_DIR / "model_routing.yaml"
    rbac_config_path: Path = CONFIG_DIR / "roles.yaml"
    drone_config_path: Path = CONFIG_DIR / "drone_configs.yaml"
    inference_timeout_seconds: float = 0.1  # 100ms budget per model call

    # --- Alerting ---
    alert_dedup_window_seconds: int = 900  # suppress dup alerts within 15 min

    # --- Redis (shared backpressure / dedup state, enables horizontal scaling) ---
    redis_url: str = "redis://localhost:6379/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()

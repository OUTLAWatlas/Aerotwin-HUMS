"""
Application entrypoint. Loads models at startup, wires up middleware
(audit logging), mounts routers, and (when run directly) serves over TLS 1.3.

Run directly:      python -m app.main
Run for dev:        uvicorn app.main:app --reload
Production:         uvicorn app.main:app --workers 4 --ssl-keyfile ... --ssl-certfile ...
                     (or terminate TLS at an ingress/load balancer and run
                     plain HTTP behind it, per docker-compose.yml comments)
"""
from __future__ import annotations

import logging
import ssl

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    routes_alerts, routes_auth, routes_config, routes_ingestion,
    routes_missions, routes_predictions, routes_ws,
)
from app.core.audit import AuditLogMiddleware
from app.core.config import get_settings
from app.ingestion.base import BackpressureQueue
from app.mission.drone_registry import get_drone_registry
from app.models_serving.registry import model_registry

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Predictive Maintenance Inference Orchestration API",
        version="1.0.0",
        description=(
            "Orchestrates ML and physics-based models for anomaly detection, "
            "fault diagnosis, and RUL estimation."
        ),
    )

    # CORS: lets the AeroTwin-HUMS dashboard (a separate origin in dev --
    # the Vite dev server on :3000) call this API from the browser. Not
    # needed for the /ws/telemetry upgrade itself (see routes_ws.py), only
    # for the plain REST endpoints below.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_middleware(AuditLogMiddleware)

    app.include_router(routes_auth.router)
    app.include_router(routes_predictions.router)
    app.include_router(routes_alerts.router)
    app.include_router(routes_missions.router)
    app.include_router(routes_ingestion.router)
    app.include_router(routes_config.router)
    app.include_router(routes_ws.router)

    @app.get("/health", tags=["health"])
    async def health():
        return {"status": "ok", "models_loaded": list(model_registry.list_loaded().keys())}

    @app.on_event("startup")
    async def on_startup():
        logger.info("Loading models into registry...")
        model_registry.load_all()

        logger.info("Loading drone/engine topology...")
        drone_registry = get_drone_registry(settings.drone_config_path)
        app.state.drone_registry = drone_registry

        try:
            from scripts.seed_users import seed
            await seed()
            logger.info("Demo users verified/seeded successfully.")
        except Exception as exc:
            logger.warning("Auto-seeding demo users skipped: %s", exc)

        app.state.ingestion_queue = BackpressureQueue(
            maxsize=settings.ingestion_queue_maxsize
        )
        logger.info("Startup complete.")

    return app


app = create_app()


def _build_ssl_context() -> ssl.SSLContext | None:
    """Builds a TLS 1.3-only context for uvicorn when certs are present.
    In most deployments TLS terminates at an ingress/load balancer instead
    (see docker/docker-compose.yml) and this returns None."""
    import os

    if not (os.path.exists(settings.tls_cert_path) and os.path.exists(settings.tls_key_path)):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(certfile=settings.tls_cert_path, keyfile=settings.tls_key_path)
    return ctx


if __name__ == "__main__":
    ssl_ctx = _build_ssl_context()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8443 if ssl_ctx else 8000,
        ssl_certfile=settings.tls_cert_path if ssl_ctx else None,
        ssl_keyfile=settings.tls_key_path if ssl_ctx else None,
        workers=4,
    )

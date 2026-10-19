import yaml
from fastapi import APIRouter, Depends, Request

from app.core.config import get_settings
from app.core.security import require_permission
from app.models_serving.registry import model_registry
from app.orchestration.rules import get_routing_config

router = APIRouter(prefix="/api/v1/config", tags=["config"])
settings = get_settings()


@router.get("/drones")
async def list_drone_configs(
    request: Request, principal=Depends(require_permission("config:read"))
):
    request.state.user_id = principal.user_id
    with open(settings.drone_config_path) as f:
        return yaml.safe_load(f)


@router.get("/models")
async def list_loaded_models(
    request: Request, principal=Depends(require_permission("config:read"))
):
    request.state.user_id = principal.user_id
    return model_registry.list_loaded()


@router.get("/routing-rules")
async def list_routing_rules(
    request: Request, principal=Depends(require_permission("config:read"))
):
    request.state.user_id = principal.user_id
    cfg = get_routing_config(settings.model_routing_config_path)
    return [
        {
            "name": r.name,
            "required_sensor_types": r.required_sensor_types,
            "mission_risk_tiers": r.mission_risk_tiers,
            "drone_types": r.drone_types,
            "sequence": [s.model for s in r.sequence],
        }
        for r in cfg.rules
    ]

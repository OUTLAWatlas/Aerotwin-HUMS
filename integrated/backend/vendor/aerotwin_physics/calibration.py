"""Loading and interpretation of the dataset-derived calibration payload.

The calibration JSON is the bridge between the reference dataset and the
configuration objects. Keeping it as data rather than hard-coded constants means
a re-calibration against new flight data is a file swap, not a code change.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from .config import (CHANNELS, FAULT_MODES, AnomalyConfig, DegradationConfig,
                     EngineConfig, FaultConfig, FaultModeConfig, NoiseConfig)
from .exceptions import CalibrationError
from .logging_utils import get_logger

logger = get_logger(__name__)

_DEFAULT_PATH = Path(__file__).with_name("data") / "calibration.json"

_REQUIRED_KEYS = ("tit_surface", "noise", "fault_signatures", "degradation_sensitivity",
                  "severity_ramp", "degradation_regimes", "residual_reference",
                  "dataset_targets")


@lru_cache(maxsize=4)
def _load_cached(path_str: str) -> Dict[str, Any]:
    path = Path(path_str)
    if not path.exists():
        raise CalibrationError(f"calibration payload not found at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CalibrationError(f"calibration payload at {path} is not valid JSON: {exc}") from exc
    missing = [k for k in _REQUIRED_KEYS if k not in payload]
    if missing:
        raise CalibrationError(f"calibration payload missing sections: {missing}")
    channels = payload["residual_reference"].get("channels", [])
    if tuple(channels) != CHANNELS:
        raise CalibrationError(
            "calibration channel order does not match aerotwin_physics.config.CHANNELS; "
            "the residual covariance would be silently transposed")
    logger.debug("loaded calibration from %s", path)
    return payload


def load_calibration(path: Optional[os.PathLike] = None) -> Dict[str, Any]:
    """Load (and cache) the calibration payload.

    Parameters
    ----------
    path:
        Optional override. Defaults to the bundled
        ``aerotwin_physics/data/calibration.json``.
    """
    return _load_cached(str(Path(path) if path is not None else _DEFAULT_PATH))


def config_from_calibration(cal: Dict[str, Any]) -> EngineConfig:
    """Turn a calibration payload into a fully-populated :class:`EngineConfig`."""
    noise = NoiseConfig(
        relative_sigma={c: cal["noise"][c]["rel_sigma"] for c in CHANNELS},
        ar1_coefficient={c: cal["noise"][c]["ar1"] for c in CHANNELS},
        reference_scale={c: cal["noise"][c]["ref_scale"] for c in CHANNELS},
    )

    ramp = cal["severity_ramp"]
    lo, hi = ramp["exponent_range"]
    modes = {
        name: FaultModeConfig(
            name=name,
            channel_gains={c: float(gains.get(c, 0.0)) for c in CHANNELS},
            ramp_exponent_range=(float(lo), float(hi)),
        )
        for name, gains in cal["fault_signatures"].items()
        if name in FAULT_MODES
    }
    missing = set(FAULT_MODES) - set(modes)
    if missing:
        raise CalibrationError(f"calibration lacks signatures for fault modes: {sorted(missing)}")

    faults = FaultConfig(
        modes=modes,
        degradation_gains={c: float(cal["degradation_sensitivity"].get(c, 0.0))
                           for c in CHANNELS},
    )

    regimes = {
        key: {"drift": float(v["rate_mean"]),
              "volatility": float(v["rate_sd"]),
              "x0": float(v["deg0_mean"])}
        for key, v in cal["degradation_regimes"].items()
    }
    # A negative fitted drift on the healthy family is estimator noise around a
    # true zero, not real healing. Clamp it: a negative drift makes the
    # first-hitting-time distribution improper downstream.
    for v in regimes.values():
        v["drift"] = max(v["drift"], 0.0)
        v["volatility"] = abs(v["volatility"])
    degradation = DegradationConfig(regimes=regimes)

    anomaly = AnomalyConfig(
        threshold=float(cal["dataset_targets"]["anomaly_threshold"]),
        mahalanobis_scale=float(cal["residual_reference"].get("mahalanobis_scale", 1.0)),
    )

    return EngineConfig(
        noise=noise, faults=faults, degradation=degradation, anomaly=anomaly,
        metadata={"calibration_source": "phm_synthetic_dataset_v2-2.xlsx",
                  "calibration_trajectories": 22,
                  "calibration_rows": 1100},
    )


def residual_reference(cal: Optional[Dict[str, Any]] = None):
    """Return ``(mean, covariance)`` of the healthy-flight residual distribution."""
    import numpy as np
    cal = cal or load_calibration()
    ref = cal["residual_reference"]
    return np.asarray(ref["mean"], float), np.asarray(ref["covariance"], float)


def dataset_targets(cal: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the reference metrics the validation harness compares against."""
    return (cal or load_calibration())["dataset_targets"]

"""AeroTwin physics engine for aero-piston-engine health monitoring.

A validated, deployable physics model of a 4-cylinder aero piston engine, with
fault injection, residual-based anomaly detection, physics-informed fault
isolation and Inverse-Gaussian remaining-useful-life prognostics.

Calibrated against ``phm_synthetic_dataset_v2-2.xlsx`` (1,100 samples across 22
trajectories). Eleven of the thirteen sensor channels are reproduced by exact
closed-form maps; the two turbine-inlet-temperature channels use a calibrated
quadratic surface.

Quick start
-----------
>>> from aerotwin_physics import EngineConfig, TrajectorySimulator, RealTimeMonitor
>>> config = EngineConfig.default()
>>> trajectory = TrajectorySimulator(config).simulate(
...     index=0, force_fault=True, fault_mode="ring_wear", regime="rul")
>>> outputs = RealTimeMonitor(config).replay(trajectory)
>>> outputs[-1].diagnosis.fault
'ring_wear'
"""

from __future__ import annotations

__version__ = "1.0.0"

from .atmosphere import AtmosphericState, ISAAtmosphere
from .calibration import dataset_targets, load_calibration, residual_reference
from .config import (CHANNELS, CHANNEL_UNITS, FAULT_MODES, UNKNOWN_CHANNELS,
                     AnomalyConfig, AtmosphereConfig, DegradationConfig,
                     DiagnosticsConfig, EngineConfig, FaultConfig, FaultModeConfig,
                     NoiseConfig, OperatingBounds, PrognosticsConfig, SimulationConfig)
from .degradation import DegradationState, WienerDegradation, health_index_from_distance
from .diagnostics import (GENERALISED_WEAR, NO_FAULT, Diagnosis, DiagnosticEngine,
                          FaultFingerprinter, SupervisedCrossCheck)
from .exceptions import (AeroTwinError, CalibrationError, ConfigurationError,
                         FaultDefinitionError, OperatingBoundsError, PrognosticsError,
                         ValidationError)
from .faults import (FaultInjector, FaultLibrary, FaultSchedule, describe_fault_modes)
from .logging_utils import configure_logging, get_logger
from .mission import MissionProfile, MissionProfileGenerator
from .noise import SensorNoiseModel, SensorValidator
from .prognostics import (DriftEstimator, RULEstimate, RULPredictor,
                          mission_survival_probability)
from .residuals import ResidualEngine, ResidualSample
from .sensors import (EnginePhysicsCore, OperatingPoint, SensorResponseModel)
from .simulator import (MonitorOutput, RealTimeMonitor, TrajectoryResult,
                        TrajectorySimulator)
from .validation import Check, ModelValidator, ValidationReport, load_reference_dataset

__all__ = [
    "__version__",
    # configuration
    "EngineConfig", "AtmosphereConfig", "OperatingBounds", "NoiseConfig", "FaultConfig",
    "FaultModeConfig", "DegradationConfig", "AnomalyConfig", "DiagnosticsConfig",
    "PrognosticsConfig", "SimulationConfig",
    "CHANNELS", "CHANNEL_UNITS", "UNKNOWN_CHANNELS", "FAULT_MODES",
    # physics
    "EnginePhysicsCore", "SensorResponseModel", "OperatingPoint",
    "ISAAtmosphere", "AtmosphericState",
    # faults & noise
    "FaultLibrary", "FaultInjector", "FaultSchedule", "describe_fault_modes",
    "SensorNoiseModel", "SensorValidator",
    # degradation, residuals, diagnostics, prognostics
    "WienerDegradation", "DegradationState", "health_index_from_distance",
    "ResidualEngine", "ResidualSample",
    "DiagnosticEngine", "FaultFingerprinter", "SupervisedCrossCheck", "Diagnosis",
    "NO_FAULT", "GENERALISED_WEAR",
    "RULPredictor", "RULEstimate", "DriftEstimator", "mission_survival_probability",
    # simulation & monitoring
    "TrajectorySimulator", "TrajectoryResult", "RealTimeMonitor", "MonitorOutput",
    "MissionProfileGenerator", "MissionProfile",
    # validation
    "ModelValidator", "ValidationReport", "Check", "load_reference_dataset",
    # calibration & infrastructure
    "load_calibration", "residual_reference", "dataset_targets",
    "configure_logging", "get_logger",
    # exceptions
    "AeroTwinError", "ConfigurationError", "OperatingBoundsError", "CalibrationError",
    "FaultDefinitionError", "PrognosticsError", "ValidationError",
]

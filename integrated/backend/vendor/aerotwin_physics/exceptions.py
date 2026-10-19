"""Exception hierarchy for the AeroTwin physics engine.

All errors raised by this package derive from :class:`AeroTwinError`, so an
integrating monitoring pipeline can catch every fault-tolerant failure mode with
a single ``except`` clause without swallowing genuine programming errors.
"""

from __future__ import annotations


class AeroTwinError(Exception):
    """Base class for every error raised by :mod:`aerotwin_physics`."""


class ConfigurationError(AeroTwinError):
    """Raised when a configuration file or object fails schema validation."""


class OperatingBoundsError(AeroTwinError):
    """Raised when a requested operating point lies outside the validated envelope.

    The physics maps in :mod:`aerotwin_physics.sensors` are calibrated regressions,
    not extrapolating first-principles solvers. Silently evaluating them far
    outside the calibration hull would produce confident nonsense, so the engine
    refuses (or clips, depending on ``EngineConfig.bounds_policy``) instead.
    """


class CalibrationError(AeroTwinError):
    """Raised when the bundled calibration payload is missing, stale or malformed."""


class FaultDefinitionError(AeroTwinError):
    """Raised when an unknown fault mode or an invalid fault schedule is requested."""


class PrognosticsError(AeroTwinError):
    """Raised when a Remaining-Useful-Life estimate cannot be formed.

    Most commonly this means the estimated degradation drift is non-positive, in
    which case the first-hitting-time distribution is improper and no finite RUL
    exists. Callers should surface this to the operator as "no trend detected"
    rather than as a large RUL number.
    """


class ValidationError(AeroTwinError):
    """Raised when the validation harness cannot compare against a reference set."""

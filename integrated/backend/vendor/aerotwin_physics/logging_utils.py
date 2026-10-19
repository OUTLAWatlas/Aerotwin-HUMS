"""Logging helpers.

The package follows the library convention: it *never* configures the root
logger and never adds handlers at import time. Applications opt in via
:func:`configure_logging`; otherwise the library logs into a
:class:`~logging.NullHandler` and stays silent.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator, Optional

_ROOT_NAME = "aerotwin_physics"

logging.getLogger(_ROOT_NAME).addHandler(logging.NullHandler())


def get_logger(name: str) -> logging.Logger:
    """Return a package-scoped logger.

    Parameters
    ----------
    name:
        Module name, usually ``__name__``. A leading ``aerotwin_physics.`` is
        added if absent so every logger sits under one configurable root.
    """
    if not name.startswith(_ROOT_NAME):
        name = f"{_ROOT_NAME}.{name}"
    return logging.getLogger(name)


def configure_logging(level: int = logging.INFO,
                      fmt: Optional[str] = None,
                      stream: Optional[object] = None) -> None:
    """Attach a stream handler to the package logger. For applications only.

    Safe to call more than once; repeated calls replace the handler rather than
    stacking duplicates, which is the usual cause of triplicated log lines in
    long-running monitoring services.
    """
    fmt = fmt or "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    logger = logging.getLogger(_ROOT_NAME)
    for handler in list(logger.handlers):
        if not isinstance(handler, logging.NullHandler):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


@contextmanager
def log_duration(logger: logging.Logger, label: str,
                 level: int = logging.DEBUG) -> Iterator[None]:
    """Log the wall-clock duration of a block.

    Used around the per-step hot loop so deployments can measure whether the
    twin is keeping up with the telemetry rate without adding a profiler.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        logger.log(level, "%s took %.3f ms", label, (time.perf_counter() - start) * 1e3)

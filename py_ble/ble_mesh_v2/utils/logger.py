"""
utils/logger.py — Shared structured logger for the BLE mesh stack.

All components obtain the same logger via ``from ble_mesh.utils.logger import log``.
The log level can be changed at runtime::

    from ble_mesh.utils.logger import set_level
    set_level("WARNING")
"""

import logging
import sys

_LOGGER_NAME = "ble_mesh"


def _build_logger() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d [%(levelname)-8s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger


log = _build_logger()


def set_level(level: str | int) -> None:
    """Adjust the log verbosity at runtime.

    Parameters
    ----------
    level : str | int
        A logging level name (``"DEBUG"``, ``"INFO"``, ``"WARNING"``, …) or
        the corresponding integer constant.
    """
    log.setLevel(level)
    for h in log.handlers:
        h.setLevel(level)

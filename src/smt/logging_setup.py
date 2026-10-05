"""Central logging setup."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

# Fallbacks when OpsConfig is not available yet (import-time get_logger).
# Keep in sync with OpsConfig.log_max_bytes / log_backup_count.
LOG_MAX_BYTES = 50 * 1024 * 1024
LOG_BACKUP_COUNT = 5

_CONFIGURED = False


def _rotation_limits() -> tuple[int, int]:
    try:
        from .config import get_ops

        ops = get_ops()
        return int(ops.log_max_bytes), int(ops.log_backup_count)
    except Exception:
        return LOG_MAX_BYTES, LOG_BACKUP_COUNT


def setup_logging(level: int = logging.INFO, log_dir: str = "./logs") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    fmt = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        max_bytes, backup_count = _rotation_limits()
        handlers.append(
            logging.handlers.RotatingFileHandler(
                Path(log_dir) / "smt.log",
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
        )
    except OSError:
        # If the log dir is not writable, stdout logging still works.
        pass

    logging.basicConfig(level=level, format=fmt, handlers=handlers)

    # httpx logs every request at INFO. Market data polls several products per
    # loop, which would bury the trading log in HTTP noise.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)

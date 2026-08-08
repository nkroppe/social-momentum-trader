"""Central logging setup."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_CONFIGURED = False


def setup_logging(level: int = logging.INFO, log_dir: str = "./logs") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    fmt = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(Path(log_dir) / "smt.log", encoding="utf-8"))
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

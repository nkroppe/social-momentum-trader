"""Kill switch: a file whose presence blocks new entries and flattens positions.

`touch KILL` on the host (bind-mounted into the container) to stop trading
without needing SSH into the app. Reachable from a phone via any file-drop /
webhook you wire to create that file.
"""

from __future__ import annotations

from pathlib import Path

from ..logging_setup import get_logger

log = get_logger("smt.killswitch")


class KillSwitch:
    def __init__(self, kill_file: str):
        self.path = Path(kill_file)

    def is_active(self) -> bool:
        return self.path.exists()

    def trip(self, reason: str = "") -> None:
        try:
            self.path.write_text(f"KILLED: {reason}\n", encoding="utf-8")
            log.critical("KILL SWITCH TRIPPED: %s", reason)
        except OSError as exc:
            log.critical("Failed to write kill file (%s): %s", self.path, exc)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
            log.warning("Kill switch cleared")

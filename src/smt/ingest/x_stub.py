"""X / Twitter collector (deferred, budget-capped).

Intentionally a thin stub for v1. X pay-per-use reads are the expensive line
item, so this is disabled by default (sources.yaml: x.enabled=false) and only
wired up once the paper loop is stable and a hard monthly read budget is set.
"""

from __future__ import annotations

from ..config import Settings, UniverseConfig, XSource
from ..logging_setup import get_logger
from ..models import SocialEvent

log = get_logger("smt.ingest.x")


class XCollector:
    source_name = "x"

    def __init__(self, settings: Settings, cfg: XSource, universe: UniverseConfig):
        self.settings = settings
        self.cfg = cfg
        self.universe = universe
        self._reads_used = 0

    def collect(self) -> list[SocialEvent]:
        # Phase-2: implement watchlist recent-search here with a hard budget check:
        #   if self._reads_used >= self.settings.x_monthly_read_budget: return []
        # Use official API only; tag events with extract_tickers(); weight ~2.0.
        log.debug("X collector is a stub (deferred); returning no events")
        return []

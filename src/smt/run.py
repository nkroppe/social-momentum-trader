"""Main orchestrator: ingest -> score -> signal -> risk -> execute -> manage."""

from __future__ import annotations

import time

from .config import (
    get_risk,
    get_security,
    get_settings,
    get_sources,
    get_universe,
)
from .ingest import build_collectors
from .logging_setup import get_logger
from .ops import Alerter, KillSwitch
from .scorer import MomentumScorer
from .store import Store
from .trader.broker import build_broker
from .trader.manager import TradeManager
from .trader.risk import RiskGate
from .trader.signals import SignalEngine

log = get_logger("smt.run")

LIVE_ACK_PHRASE = "I_UNDERSTAND_LIVE_RISK"


class Runner:
    def __init__(self):
        self.settings = get_settings()
        self.risk = get_risk()
        self.universe = get_universe()
        self.sources = get_sources()
        self.security = get_security()

        self._enforce_live_latches()

        self.store = Store(self.settings.database_url)
        self.store.init_db()

        self.alerter = Alerter(self.settings)
        self.kill = KillSwitch(self.settings.kill_file)
        self.collectors = build_collectors(self.settings, self.sources, self.universe)
        self.scorer = MomentumScorer(
            self.store,
            self.universe,
            bucket_minutes=self.risk.scorer_bucket_minutes,
            lookback_buckets=self.risk.scorer_lookback_buckets,
        )
        self.signals = SignalEngine(self.risk, self.universe)
        self.broker = build_broker(self.settings)
        self.risk_gate = RiskGate(self.risk, self.store)
        self.manager = TradeManager(
            self.settings, self.risk, self.universe, self.store, self.broker
        )
        self._last_ingest = 0.0
        self._killed_notified = False

        mode = "LIVE" if self.broker.name == "coinbase" else "PAPER"
        log.info("Runner ready in %s mode (broker=%s)", mode, self.broker.name)
        self.store.add_security_event("startup", f"mode={mode} broker={self.broker.name}")

    def _enforce_live_latches(self) -> None:
        """Second latch: force paper unless LIVE and the ack phrase are both set."""
        if self.settings.live and self.settings.live_ack != LIVE_ACK_PHRASE:
            log.critical(
                "LIVE=true but LIVE_ACK != %s -> forcing PAPER mode for safety.",
                LIVE_ACK_PHRASE,
            )
            # Mutate the cached settings object so downstream build_broker sees paper.
            self.settings.live = False

    # ---- one iteration -----------------------------------------------------

    def ingest(self) -> int:
        total = 0
        for c in self.collectors:
            try:
                events = c.collect()
                inserted = self.store.add_events(events)
                total += inserted
                log.debug("collector %s: %d new events", c.source_name, inserted)
            except Exception as exc:  # noqa: BLE001
                log.warning("collector %s failed: %s", c.source_name, exc)
        if total:
            log.info("ingested %d new events", total)
        return total

    def evaluate_and_trade(self) -> None:
        equity = self.manager.equity()
        scores = self.scorer.score_all()
        candidates = self.signals.candidates(scores)
        for cand in candidates:
            decision = self.risk_gate.evaluate(cand, equity, self.settings.paper_start_equity)
            if not decision.approved:
                log.info("REJECT %s: %s", cand.ticker, decision.reason)
                continue
            self.manager.open_position(cand, decision.notional_usd)
            equity = self.manager.equity()  # refresh after deploying capital

    def step(self) -> None:
        # 1) Kill switch: flatten and block entries.
        if self.kill.is_active():
            if not self._killed_notified:
                self.alerter.notify(
                    "Kill switch active",
                    "Flattening positions, no new entries.",
                    critical=True,
                )
                self.store.add_security_event("kill_switch", "active", "CRITICAL")
                self._killed_notified = True
            self.manager.manage_open_trades(force_flatten=True)
            return
        self._killed_notified = False

        # 2) Ingest on its own cadence.
        now = time.monotonic()
        if now - self._last_ingest >= self.sources.poll_interval_seconds:
            self.ingest()
            self._last_ingest = now

        # 3) Signals -> risk -> entries.
        self.evaluate_and_trade()

        # 4) Manage exits every loop.
        self.manager.manage_open_trades()

    def run_forever(self) -> None:
        log.info("Starting main loop (interval=%ds)", self.settings.loop_interval_seconds)
        # Prime with an initial ingest so scores have data.
        self.ingest()
        self._last_ingest = time.monotonic()
        while True:
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001
                log.exception("loop error: %s", exc)
                self.alerter.notify("Loop error", str(exc))
            time.sleep(self.settings.loop_interval_seconds)


def main() -> None:
    Runner().run_forever()

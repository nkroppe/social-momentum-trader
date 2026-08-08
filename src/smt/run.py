"""Main orchestrator: ingest -> score -> signal -> risk -> execute -> manage."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from .config import (
    CONFIG_DIR,
    LIVE_ACK_PHRASE,
    get_market,
    get_ops,
    get_risk,
    get_security,
    get_settings,
    get_signals,
    get_sources,
    get_strategies,
    get_universe,
)
from .ingest import build_collectors
from .logging_setup import get_logger
from .market import MarketData
from .ops import Alerter, KillSwitch
from .ops.reports import build_weekly_report
from .ops.schedule import WeeklyScheduler
from .ops.soak import SoakTracker
from .scorer import MomentumScorer
from .store import Store
from .trader.broker import build_broker
from .trader.manager import TradeManager
from .trader.risk import RiskGate
from .trader.signals import SignalEngine

log = get_logger("smt.run")


class Runner:
    """Orchestrates the trading loop.

    `offline=True` disables all market data, which makes `smt simulate` and
    offline dev deterministic and credential-free at the cost of skipping every
    price gate. Never use it for a soak.
    """

    def __init__(self, offline: bool = False):
        self.settings = get_settings()
        self.risk = get_risk()
        self.universe = get_universe()
        self.sources = get_sources()
        self.security = get_security()
        self.ops = get_ops()
        self.signals = get_signals()
        self.strategies = get_strategies().enabled()
        self.offline = offline

        self.market_cfg = get_market()
        if offline:
            # Make the absence of price gating explicit rather than relying on
            # a None provider slipping past them.
            self.market_cfg = self.market_cfg.model_copy(deep=True)
            self.market_cfg.confirmation.enabled = False
            self.market_cfg.confirmation.fail_closed = False
            self.market_cfg.regime.enabled = False
            self.market_cfg.sizing.enabled = False

        self._enforce_live_latches()

        sources_path = CONFIG_DIR / "sources.yaml"
        if not sources_path.exists():
            raise FileNotFoundError(
                f"Missing {sources_path}. Restore config/ from git "
                f"(git checkout config/) and ensure docker mounts ./config to /app/config."
            )

        self.store = Store(self.settings.database_url)
        self.store.init_db()

        self.alerter = Alerter(self.settings)
        self.kill = KillSwitch(self.settings.kill_file)
        self.collectors = build_collectors(self.settings, self.sources, self.universe)

        self.market = None if offline else MarketData(self.market_cfg)

        # One scorer + signal engine per strategy (own bucket/lookback + thresholds).
        self.scorers = {
            st.name: MomentumScorer(
                self.store,
                self.universe,
                bucket_minutes=st.scorer_bucket_minutes,
                lookback_buckets=st.scorer_lookback_buckets,
                seasonal_days=st.scorer_seasonal_days,
                seasonal_min_history_hours=st.scorer_seasonal_min_history_hours,
            )
            for st in self.strategies
        }
        self.signal_engines = {
            st.name: SignalEngine(
                st, self.universe, self.signals, self.market, self.market_cfg
            )
            for st in self.strategies
        }
        # A general-purpose scorer (global defaults) for the `score` CLI/demos.
        self.scorer = MomentumScorer(
            self.store,
            self.universe,
            bucket_minutes=self.risk.scorer_bucket_minutes,
            lookback_buckets=self.risk.scorer_lookback_buckets,
            seasonal_days=self.risk.scorer_seasonal_days,
            seasonal_min_history_hours=self.risk.scorer_seasonal_min_history_hours,
        )

        paper_market = self.market if self.market_cfg.paper_use_real_prices else None
        self.broker = build_broker(self.settings, paper_market)
        self.risk_gate = RiskGate(self.store, self.signals, self.market_cfg)
        self.manager = TradeManager(
            self.settings,
            self.universe,
            self.store,
            self.broker,
            self.market,
            self.market_cfg,
            alerter=self.alerter,
            trade_alerts=self.ops.trade_alerts,
        )
        self.soak = SoakTracker(Path(self.ops.soak.state_file))
        self.weekly = WeeklyScheduler(self.ops.weekly_report)
        self._last_ingest = 0.0
        self._last_digest = 0.0
        self._digest_interval_s = self.ops.soak.digest_interval_hours * 3600
        self._killed_notified = False
        self._halt_notified: set[str] = set()

        if self.broker.name == "paper":
            self.soak.ensure_started("paper")

        mode = "LIVE" if self.broker.name == "coinbase" else "PAPER"
        names = ", ".join(f"{st.name}({st.allocation:.0%})" for st in self.strategies)
        log.info(
            "Config dir=%s | reddit=%s x=%s mock=%s",
            CONFIG_DIR,
            self.sources.reddit.enabled,
            self.sources.x.enabled,
            self.sources.mock.enabled,
        )
        if self.market is None:
            log.warning("OFFLINE: price confirmation, regime filter, and ATR exits are disabled")
        else:
            regime_ok, regime_detail = self.market.regime_ok()
            log.info("Regime: %s (%s)", "RISK-ON" if regime_ok else "RISK-OFF", regime_detail)
            tiers = ", ".join(
                f"{t}={self.universe.tier_of(t, self.signals.default_tier)}"
                for t in self.universe.symbols
            )
            log.info("Universe tiers: %s", tiers)
        log.info(
            "Runner ready in %s mode (broker=%s) strategies=[%s]",
            mode,
            self.broker.name,
            names,
        )
        self.store.add_security_event(
            "startup", f"mode={mode} broker={self.broker.name} strategies=[{names}]"
        )

    def _enforce_live_latches(self) -> None:
        """Second latch: force paper unless LIVE and the ack phrase are both set."""
        if self.settings.live and self.settings.live_ack != LIVE_ACK_PHRASE:
            log.critical(
                "LIVE=true but LIVE_ACK != %s -> forcing PAPER mode for safety.",
                LIVE_ACK_PHRASE,
            )
            self.settings.live = False
            return

        if self.settings.live:
            tracker = SoakTracker(Path(self.ops.soak.state_file))
            min_days = self.security.min_paper_soak_days
            if not tracker.meets_minimum(min_days):
                log.critical(
                    "LIVE=true but paper soak only %.1f days (need %d) -> forcing PAPER.",
                    tracker.days_elapsed(),
                    min_days,
                )
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
        """Evaluate each strategy independently against its own allocation."""
        for st in self.strategies:
            start_alloc = self.manager.allocation_start_equity(st)
            halted, why = self.risk_gate.portfolio_halted(st, start_alloc)
            if halted:
                if st.name not in self._halt_notified:
                    self.alerter.notify(
                        f"Loss halt [{st.name}]",
                        why,
                        critical=True,
                    )
                    self.store.add_security_event("loss_halt", f"{st.name}: {why}", "WARNING")
                    self._halt_notified.add(st.name)
                continue
            self._halt_notified.discard(st.name)

            equity_alloc = self.manager.allocation_equity(st)
            scores = self.scorers[st.name].score_all()
            candidates = self.signal_engines[st.name].candidates(scores)
            for cand in candidates:
                decision = self.risk_gate.evaluate(cand, st, equity_alloc, start_alloc)
                if not decision.approved:
                    log.info("REJECT[%s] %s: %s", st.name, cand.ticker, decision.reason)
                    continue
                self.manager.open_position(cand, decision.notional_usd, st)
                equity_alloc = self.manager.allocation_equity(st)

    def _send_digest(self) -> None:
        """Periodic soak summary to configured alert channels (non-critical)."""
        lines = [
            self.soak.summary_line(self.security.min_paper_soak_days),
            f"Mode: {'LIVE' if self.broker.name == 'coinbase' else 'PAPER'}",
            "",
        ]
        for st in self.strategies:
            s = self.store.strategy_stats(st.name)
            alloc_eq = self.manager.allocation_equity(st)
            lines.append(
                f"[{st.name}] alloc_eq=${alloc_eq:.2f} open={s['open_positions']} "
                f"closed={s['closed_trades']} win={s['win_rate']:.0%} "
                f"pnl=${s['total_pnl']:.2f} pnl_24h=${s['day_pnl']:.2f}"
            )
        body = "\n".join(lines)
        self.alerter.notify("Daily soak digest", body, critical=False)
        log.info("Sent soak digest")

    def weekly_report(self, occurrence: datetime | None = None) -> tuple[str, str]:
        """Build the report for `occurrence` (defaults to the latest due window)."""
        occurrence = occurrence or self.weekly.previous_occurrence()
        start, end = self.weekly.report_window(occurrence)
        return build_weekly_report(
            self.store,
            [st.name for st in self.strategies],
            start,
            end,
            self.weekly.tz,
            mode="LIVE" if self.broker.name == "coinbase" else "PAPER",
            max_trades_listed=self.ops.weekly_report.max_trades_listed,
            mark_price=self.broker.current_price,
        )

    def _send_weekly_if_due(self) -> None:
        occurrence = self.weekly.due()
        if occurrence is None:
            return
        subject, body = self.weekly_report(occurrence)
        self.alerter.notify(subject, body, critical=False)
        # Only mark after a successful build+send so a crash retries next loop.
        self.weekly.mark_sent(occurrence)
        log.info("Sent weekly report for week ending %s", occurrence.isoformat())

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
        self.ingest()
        self._last_ingest = time.monotonic()
        self._last_digest = time.monotonic()
        self.weekly.ensure_initialized()
        if self.ops.weekly_report.enabled:
            log.info("Next weekly report: %s", self.weekly.next_occurrence().isoformat())
        while True:
            try:
                self.step()
                if time.monotonic() - self._last_digest >= self._digest_interval_s:
                    self._send_digest()
                    self._last_digest = time.monotonic()
                self._send_weekly_if_due()
            except Exception as exc:  # noqa: BLE001
                log.exception("loop error: %s", exc)
                self.alerter.notify("Loop error", str(exc))
            time.sleep(self.settings.loop_interval_seconds)


def main() -> None:
    Runner().run_forever()

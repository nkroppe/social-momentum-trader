"""Main orchestrator: ingest -> score -> signal -> risk -> execute -> manage."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
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
from .llm import LLMCoordinator, get_llm
from .logging_setup import get_logger
from .market import MarketData
from .models import utcnow
from .ops import Alerter, KillSwitch, TelegramControl
from .ops.reports import build_weekly_report
from .ops.schedule import WeeklyScheduler
from .ops.soak import SoakTracker
from .policy import trading_policy_identity
from .scorer import MomentumScorer
from .store import (
    OPPORTUNITY_LEDGER_VERSION,
    Store,
    opportunity_key,
    stable_config_fingerprint,
)
from .trader.broker import build_broker
from .trader.manager import TradeManager
from .trader.paper import PaperMarketUnavailable, PaperOrderRejected
from .trader.risk import RiskGate
from .trader.signals import SignalEngine, SignalEvaluation

log = get_logger("smt.run")


class Runner:
    """Orchestrates the trading loop.

    `offline=True` disables all market data, which makes `smt simulate` and
    offline dev deterministic and credential-free at the cost of skipping every
    price gate. Never use it for a soak.
    """

    def __init__(
        self,
        offline: bool = False,
        *,
        config_fingerprint: str | None = None,
        run_id: str | None = None,
    ):
        self.settings = get_settings()
        self.risk = get_risk()
        self.universe = get_universe()
        self.sources = get_sources()
        self.security = get_security()
        self.ops = get_ops()
        self.signals = get_signals()
        self.llm_cfg = get_llm()
        strategies_config = get_strategies()
        self.strategies = strategies_config.enabled()
        self.market_cfg = get_market()
        self.policy_identity = trading_policy_identity(
            strategies=strategies_config,
            risk=self.risk,
            market=self.market_cfg,
            signals=self.signals,
            universe=self.universe,
            sources=self.sources,
            llm=self.llm_cfg,
        )
        self.offline = offline
        self.config_fingerprint = (
            stable_config_fingerprint(config_fingerprint)
            if config_fingerprint is not None
            else self.policy_identity.fingerprint
        )
        self.run_id = run_id or uuid.uuid4().hex

        if offline:
            # Make the absence of price gating explicit rather than relying on
            # a None provider slipping past them.
            self.market_cfg = self.market_cfg.model_copy(deep=True)
            self.market_cfg.confirmation.enabled = False
            self.market_cfg.confirmation.fail_closed = False
            self.market_cfg.regime.enabled = False
            self.market_cfg.sizing.enabled = False
            self.market_cfg.price_action_enabled = False
            self.market_cfg.price_action_fail_closed = False

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
        self.telegram_control = TelegramControl(self.settings, self.ops.telegram_control)
        self.collectors = build_collectors(self.settings, self.sources, self.universe, self.store)

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
            st.name: SignalEngine(st, self.universe, self.signals, self.market, self.market_cfg)
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
        self.broker = build_broker(
            self.settings,
            paper_market,
            offline_simulation=offline,
        )
        self.risk_gate = RiskGate(
            self.store,
            self.signals,
            self.market_cfg,
            mark_price=self.market.price if self.market is not None else self.broker.current_price,
            quote=(
                self.market.quote
                if self.market is not None
                else getattr(self.broker, "execution_quote", None)
            ),
            portfolio_equity=lambda: self.manager.equity(),
            risk=self.risk,
            universe=self.universe,
        )
        self.manager = TradeManager(
            self.settings,
            self.universe,
            self.store,
            self.broker,
            self.market,
            self.market_cfg,
            alerter=self.alerter,
            trade_alerts=self.ops.trade_alerts,
            strategies=self.strategies,
            config_fingerprint=self.config_fingerprint,
        )
        self.soak = SoakTracker(Path(self.ops.soak.state_file))
        self.weekly = WeeklyScheduler(self.ops.weekly_report)
        self.llm = LLMCoordinator(
            self.llm_cfg,
            self.store,
            self.alerter,
            self.strategies,
            self.signals,
            self.market_cfg,
            offline=offline,
        )
        self._last_ingest = 0.0
        self._last_digest = 0.0
        self._digest_interval_s = self.ops.soak.digest_interval_hours * 3600
        self._killed_notified = False
        self._halt_notified: set[str] = set()
        self._setup_sample_cooldown_s = max(self.sources.x.count_window_minutes, 1) * 60

        if self.broker.name == "paper" and not self.offline:
            self.soak.ensure_started(
                "paper",
                fingerprint=self.config_fingerprint,
                manifest=self.policy_identity.manifest,
            )

        mode = "LIVE" if self.broker.name == "coinbase" else "PAPER"
        names = ", ".join(
            f"{st.name}({st.allocation:.0%}, exit={st.exit_profile.label})"
            for st in self.strategies
        )
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
            "Runner ready in %s mode (broker=%s) policy=%s strategies=[%s]",
            mode,
            self.broker.name,
            self.config_fingerprint[:12],
            names,
        )
        self.store.add_security_event(
            "startup",
            f"mode={mode} broker={self.broker.name} "
            f"policy={self.config_fingerprint[:12]} strategies=[{names}]",
        )

    def _enforce_live_latches(self) -> None:
        """Second latch: force paper unless LIVE and the ack phrase are both set."""
        if self.settings.live and any(st.advanced_exit_enabled for st in self.strategies):
            raise RuntimeError(
                "LIVE startup blocked: advanced partial/chandelier exits are PAPER-only "
                "until Coinbase server-side bracket adjustment parity is implemented."
            )
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
            if not tracker.meets_minimum(min_days, self.config_fingerprint):
                log.critical(
                    "LIVE=true but current-policy paper soak is not ready "
                    "(policy=%s, %.1f days, need %d) -> forcing PAPER.",
                    self.config_fingerprint[:12],
                    tracker.days_elapsed(self.config_fingerprint),
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

    def _ensure_social_sample(self, ticker: str) -> None:
        """Persist a post sample when a candidate has no recent social_events."""
        recent = self.store.recent_social_events(ticker, limit=1)
        if recent:
            created = recent[0].created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age = (utcnow() - created).total_seconds()
            if age < self._setup_sample_cooldown_s:
                return
        for collector in getattr(self, "collectors", ()) or ():
            sample = getattr(collector, "sample_for_ticker", None)
            if sample is None:
                continue
            try:
                events = sample(ticker)
            except Exception as exc:  # noqa: BLE001
                log.warning("setup sample %s failed: %s", ticker, exc)
                continue
            if not events:
                continue
            inserted = self.store.add_events(events)
            log.info(
                "setup-triggered sample %s: %d events (%d new)",
                ticker,
                len(events),
                inserted,
            )
            return

    def evaluate_and_trade(self) -> None:
        """Evaluate each strategy independently against its own allocation."""
        for st in self.strategies:
            start_alloc = self.manager.allocation_start_equity(st)
            scores = self.scorers[st.name].score_all()
            engine = self.signal_engines[st.name]
            if hasattr(engine, "evaluations"):
                evaluations = engine.evaluations(scores)
                for evaluation in evaluations:
                    self._persist_evaluation(evaluation)
                candidates = engine.ranked_candidates(evaluations)
            else:
                # Compatibility for focused callers that provide a minimal engine.
                candidates = engine.candidates(scores)

            halted, why = self.risk_gate.portfolio_halted(st, start_alloc)
            if halted:
                for candidate in candidates:
                    if candidate.opportunity_key:
                        self.store.enrich_opportunity(
                            candidate.opportunity_key,
                            risk_status="portfolio_halted",
                            risk_reason=why,
                            execution_status="not_applicable",
                            execution_reason="portfolio halted before entry review",
                        )
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
            for cand in candidates:
                self._ensure_social_sample(cand.ticker)
                self._audit_candidate(cand, risk_status="not_evaluated")
                llm_passed = self.llm.review_candidate(cand)
                if cand.opportunity_key:
                    self.store.enrich_opportunity(
                        cand.opportunity_key,
                        llm_status=cand.llm_status or "not_evaluated",
                        llm_score=cand.llm_score,
                        llm_veto=cand.llm_veto,
                        llm_reason=cand.llm_reason,
                        shadow_decision_key=cand.decision_key,
                    )
                if not llm_passed:
                    self._audit_candidate(cand, risk_status="blocked_by_llm")
                    if cand.opportunity_key:
                        self.store.enrich_opportunity(
                            cand.opportunity_key,
                            risk_status="blocked_by_llm",
                            risk_reason=cand.llm_reason or "LLM review rejected candidate",
                            execution_status="not_applicable",
                            execution_reason="blocked before execution",
                        )
                    continue
                decision = self.risk_gate.evaluate(cand, st, equity_alloc, start_alloc)
                self._audit_candidate(
                    cand,
                    risk_status="approved" if decision.approved else "rejected",
                    risk_reason=decision.reason,
                )
                if cand.opportunity_key:
                    projection = decision.projection
                    self.store.enrich_opportunity(
                        cand.opportunity_key,
                        risk_status="approved" if decision.approved else "rejected",
                        risk_reason=decision.reason,
                        proposed_entry_price=cand.entry_price or None,
                        proposed_stop_price=cand.structure_stop or None,
                        proposed_notional_usd=decision.notional_usd or None,
                        proposed_risk_usd=decision.risk_budget_usd or None,
                        portfolio_equity=projection.equity if projection else None,
                        portfolio_existing_heat=(projection.existing_heat if projection else None),
                        portfolio_proposed_heat=(projection.proposed_heat if projection else None),
                        portfolio_gross_exposure=(
                            projection.gross_exposure if projection else None
                        ),
                        portfolio_symbol_exposure=(
                            projection.symbol_exposure if projection else None
                        ),
                        portfolio_micro_exposure=(
                            projection.micro_exposure if projection else None
                        ),
                    )
                if not decision.approved:
                    log.info("REJECT[%s] %s: %s", st.name, cand.ticker, decision.reason)
                    if cand.opportunity_key:
                        self.store.enrich_opportunity(
                            cand.opportunity_key,
                            execution_status="not_applicable",
                            execution_reason="risk rejected candidate",
                        )
                    continue
                try:
                    trade = self.manager.open_position(
                        cand,
                        decision.notional_usd,
                        st,
                        risk_budget_usd=decision.risk_budget_usd,
                    )
                except (PaperOrderRejected, PaperMarketUnavailable) as exc:
                    log.warning("REJECT[%s] %s: PAPER execution: %s", st.name, cand.ticker, exc)
                    self._audit_candidate(
                        cand,
                        risk_status="rejected",
                        risk_reason=f"PAPER execution: {exc}",
                    )
                    if cand.opportunity_key:
                        self.store.enrich_opportunity(
                            cand.opportunity_key,
                            execution_status="rejected",
                            execution_reason=f"PAPER execution: {exc}",
                        )
                    continue
                self.store.link_shadow_trade(cand.decision_key, trade.id)
                if cand.opportunity_key:
                    self.store.enrich_opportunity(
                        cand.opportunity_key,
                        execution_status="opened",
                        execution_reason="position opened",
                        trade_id=trade.id,
                    )
                equity_alloc = self.manager.allocation_equity(st)

    def _persist_evaluation(self, evaluation: SignalEvaluation) -> None:
        """Create the immutable candle identity and refresh deterministic evidence."""
        fingerprint = getattr(self, "config_fingerprint", stable_config_fingerprint())
        run_id = getattr(self, "run_id", "compatibility-run")
        key = opportunity_key(
            config_fingerprint=fingerprint,
            run_id=run_id,
            strategy=evaluation.strategy,
            ticker=evaluation.ticker,
            trigger_candle_ts=evaluation.trigger_candle_ts,
        )
        candidate = evaluation.candidate
        if candidate is not None:
            candidate.opportunity_key = key
        candidate_status = candidate is not None
        self.store.upsert_opportunity(
            opportunity_key=key,
            ledger_version=OPPORTUNITY_LEDGER_VERSION,
            config_fingerprint=fingerprint,
            run_id=run_id,
            strategy=evaluation.strategy,
            ticker=evaluation.ticker,
            product_id=evaluation.product_id,
            tier=evaluation.tier,
            trigger_granularity_seconds=evaluation.trigger_granularity_seconds,
            trigger_candle_ts=evaluation.trigger_candle_ts,
            trigger_closed_at=evaluation.trigger_closed_at,
            outcome_status=evaluation.outcome_status,
            outcome_reason=evaluation.outcome_reason,
            regime_status=evaluation.regime_status,
            regime_reason=evaluation.regime_reason,
            price_status=evaluation.price_status,
            price_reason=evaluation.price_reason,
            setup_status=evaluation.setup_status,
            setup_name=evaluation.setup_name,
            setup_reason=evaluation.setup_reason,
            confirmation_status=evaluation.confirmation_status,
            confirmation_reason=evaluation.confirmation_reason,
            social_status=evaluation.social_status,
            social_reason=evaluation.social_reason,
            llm_status="not_evaluated" if candidate_status else "not_applicable",
            risk_status="not_evaluated" if candidate_status else "not_applicable",
            execution_status="not_evaluated" if candidate_status else "not_applicable",
            feature_snapshot=evaluation.feature_snapshot,
            proposed_entry_price=(
                candidate.entry_price if candidate and candidate.entry_price > 0 else None
            ),
            proposed_stop_price=(
                candidate.structure_stop if candidate and candidate.structure_stop > 0 else None
            ),
        )

    def _audit_candidate(
        self,
        candidate,
        *,
        risk_status: str,
        risk_reason: str = "",
    ) -> None:
        """Upsert the latest social/LLM/risk view for one stable setup."""
        self.store.upsert_shadow_decision(
            decision_key=candidate.decision_key,
            opportunity_key=candidate.opportunity_key,
            ticker=candidate.ticker,
            strategy=candidate.strategy,
            tier=candidate.tier,
            decision_mode=candidate.decision_mode,
            setup=candidate.setup,
            count_volume=candidate.count_volume,
            engagement=candidate.engagement,
            social_decision=candidate.social_decision,
            social_reason=candidate.social_reason,
            llm_status=candidate.llm_status or "not_evaluated",
            llm_score=candidate.llm_score,
            llm_veto=candidate.llm_veto,
            llm_reason=candidate.llm_reason,
            risk_status=risk_status,
            risk_reason=risk_reason,
        )

    def mature_opportunities(self) -> int:
        """Apply only due, post-evaluation candle outcomes to prospective rows."""
        if getattr(self, "market", None) is None or not hasattr(self, "store"):
            return 0
        matured = 0
        now = utcnow()
        for row in self.store.pending_opportunity_maturations():
            evaluated = row.evaluated_at
            evaluated = evaluated if evaluated.tzinfo else evaluated.replace(tzinfo=now.tzinfo)
            if now < evaluated or not row.product_id:
                continue
            candles = self.market.candles(
                row.product_id,
                row.trigger_granularity_seconds,
            )
            if candles and self.store.mature_opportunity(
                row.opportunity_key,
                candles,
                as_of=now,
            ):
                matured += 1
        return matured

    def _send_digest(self) -> None:
        """Periodic soak summary to configured alert channels (non-critical)."""
        lines = [
            self.soak.summary_line(
                self.security.min_paper_soak_days,
                self.config_fingerprint,
            ),
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
        start, end = self.weekly.report_window(occurrence)
        self.llm.request_weekly_reflection(
            occurrence=occurrence,
            start=start,
            end=end,
            report_subject=subject,
            report_body=body,
        )
        if not self.alerter.notify(subject, body, critical=False):
            log.warning(
                "Weekly report for %s was not delivered; will retry next loop",
                occurrence.isoformat(),
            )
            return
        self.weekly.mark_sent(occurrence)
        log.info("Sent weekly report for week ending %s", occurrence.isoformat())

    def step(self) -> None:
        # Shadow reviews are observational and should finish even while the
        # kill switch is holding the trading path flat.
        self.llm.poll_judgements()
        self.mature_opportunities()

        # 0) Phone control: exact Telegram KILL / START from the configured chat.
        try:
            applied = self.telegram_control.poll_and_apply(self.kill, self.alerter)
        except Exception as exc:  # noqa: BLE001 - never let control polling abort the loop
            log.warning("telegram control poll failed: %s", exc)
            applied = []
        if "KILL" in applied:
            self.store.add_security_event(
                "kill_switch",
                "tripped via telegram KILL",
                "CRITICAL",
            )
        elif "START" in applied:
            self.store.add_security_event(
                "kill_switch",
                "cleared via telegram START",
                "WARNING",
            )
            self._killed_notified = False

        # 1) Kill switch: flatten and block entries.
        if self.kill.is_active():
            if not self._killed_notified:
                # Telegram KILL already notifies; file/CLI trips still need one.
                if "KILL" not in applied:
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

        # 3) Manage exits before entries so a transient quote miss on the entry
        # path cannot skip bar-based stop/TP handling for open positions.
        self.manager.manage_open_trades()

        # 4) Signals -> risk -> entries.
        try:
            self.evaluate_and_trade()
        except PaperMarketUnavailable as exc:
            log.warning("entry evaluation skipped (market unavailable): %s", exc)

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
                self.llm.poll_reflection()
            except Exception as exc:  # noqa: BLE001
                log.exception("loop error: %s", exc)
                self.alerter.notify("Loop error", str(exc))
            time.sleep(self.settings.loop_interval_seconds)


def main() -> None:
    Runner().run_forever()

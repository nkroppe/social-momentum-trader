"""Command-line interface: run, status, score, simulate, kill switch controls."""

from __future__ import annotations

import argparse

from . import __version__
from .logging_setup import get_logger

log = get_logger("smt.cli")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _cmd_run(_args: argparse.Namespace) -> int:
    from .run import Runner

    Runner().run_forever()
    return 0


def _cmd_init_db(_args: argparse.Namespace) -> int:
    from .config import get_settings
    from .store import Store

    Store(get_settings().database_url).init_db()
    print("Database initialized.")
    return 0


def _cmd_score(_args: argparse.Namespace) -> int:
    """Ingest once, then show social scores next to the price gates."""
    from .run import Runner

    r = Runner()
    r.ingest()

    if r.market is not None:
        ok, detail = r.market.regime_ok()
        print(f"Regime: {'RISK-ON' if ok else 'RISK-OFF'} ({detail})\n")

    header = (
        f"{'TICKER':<8}{'TIER':<8}{'MODE':<8}{'ZSCORE':>8}{'MENTIONS':>9}"
        f"{'AUTHORS':>8}{'BULL%':>7}{'RET':>8}{'ATR%':>7}{'>SMA':>6}"
    )
    print(header)
    print("-" * len(header))

    for s in r.scorer.score_all():
        tier_name = r.universe.tier_of(s.ticker, r.signals.default_tier)
        tier = r.signals.tier(tier_name)
        ret = atr_pct = 0.0
        above = "n/a"
        if r.market is not None:
            snap = r.market.snapshot(
                r.universe.symbols[s.ticker].product_id,
                sma_periods=r.market_cfg.confirmation.sma_periods,
                lookback_periods=max(1, r.risk.confirm_lookback_hours),
            )
            if snap.ok:
                ret, atr_pct = snap.trailing_return, snap.atr_pct
                above = "yes" if snap.above_sma else "no"
            else:
                above = "err"
        print(
            f"{s.ticker:<8}{tier_name:<8}{tier.signal_mode:<8}{s.zscore:>8.2f}"
            f"{s.mentions_window:>9d}{s.distinct_authors:>8d}{s.bullish_ratio * 100:>7.0f}"
            f"{ret * 100:>7.2f}%{atr_pct * 100:>6.2f}%{above:>6}"
        )
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    from .run import Runner

    r = Runner()
    print(f"Mode  : {'LIVE' if r.settings.live else 'PAPER'}")
    print(f"Policy: {r.policy_identity.fingerprint[:12]}")
    print(f"Total equity : ${r.manager.equity():.2f}")
    for st in r.strategies:
        open_trades = r.store.open_trades(st.name)
        alloc_eq = r.manager.allocation_equity(st)
        print(
            f"\n[{st.name}] allocation={st.allocation:.0%} "
            f"alloc_equity=${alloc_eq:.2f} open={len(open_trades)} "
            f"exit={st.exit.label}"
        )
        for t in open_trades:
            print(
                f"  - {t.ticker:<6} qty={t.qty:.6f} entry={t.entry_price:.6f} "
                f"tp={t.take_profit:.6f} sl={t.stop_loss:.6f} "
                f"mfe={t.mfe_r:.2f}R held={t.hold_hours:.1f}h "
                f"profile={t.exit_profile_label or 'legacy'} "
                f"policy={(t.config_fingerprint or 'legacy')[:12]}"
            )
    return 0


def _cmd_compare(_args: argparse.Namespace) -> int:
    """Side-by-side performance of each strategy over the soak."""
    from .run import Runner

    r = Runner()
    print(f"Mode: {'LIVE' if r.settings.live else 'PAPER'}  |  Comparing strategies\n")
    header = (
        f"{'STRATEGY':<10}{'ALLOC':>7}{'ALLOC_EQ':>12}{'OPEN':>6}"
        f"{'CLOSED':>8}{'WINRATE':>9}{'PNL':>10}{'PNL_24H':>10}{'AVG_HOLD_H':>12}"
    )
    print(header)
    print("-" * len(header))
    for st in r.strategies:
        s = r.store.strategy_stats(st.name)
        alloc_eq = r.manager.allocation_equity(st)
        print(
            f"{st.name:<10}{st.allocation:>6.0%} {alloc_eq:>11.2f}{s['open_positions']:>6}"
            f"{s['closed_trades']:>8}{s['win_rate']:>8.0%} {s['total_pnl']:>9.2f}"
            f"{s['day_pnl']:>10.2f}{s['avg_hold_hours']:>12.2f}"
        )
    return 0


def _cmd_kill(args: argparse.Namespace) -> int:
    from .config import get_settings
    from .ops import KillSwitch

    KillSwitch(get_settings().kill_file).trip(args.reason or "manual")
    print("Kill switch tripped.")
    return 0


def _cmd_clear_kill(_args: argparse.Namespace) -> int:
    from .config import get_settings
    from .ops import KillSwitch

    KillSwitch(get_settings().kill_file).clear()
    print("Kill switch cleared.")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .ops.preflight import all_passed, run_preflight

    if args.live:
        profile = "live"
    elif args.dev:
        profile = "dev"
    else:
        profile = "production"
    results = run_preflight(profile)
    print(f"Preflight profile: {profile}\n")
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        print(f"[{mark}] {r.name}: {r.detail}")
    if all_passed(results):
        print("\nAll checks passed.")
        return 0
    print("\nOne or more checks failed.")
    return 1


def _cmd_test_alerts(_args: argparse.Namespace) -> int:
    from .config import get_settings
    from .ops import Alerter

    Alerter(get_settings()).notify(
        "SMT test alert",
        "If you received this, alert channels are working.",
        critical=True,
    )
    print("Test alert dispatched (check email / ntfy / Telegram).")
    return 0


def _cmd_soak_report(args: argparse.Namespace) -> int:
    from .run import Runner

    r = Runner()
    print(r.soak.summary_line(r.security.min_paper_soak_days))
    print()
    return _cmd_compare(args)


def _cmd_weekly_report(args: argparse.Namespace) -> int:
    """Print the weekly report, and optionally deliver it now.

    Defaults to the week in progress, which is what you want when checking on
    demand; the scheduled send always covers a completed week.
    """
    from .run import Runner

    r = Runner()
    occurrence = r.weekly.previous_occurrence() if args.last else r.weekly.next_occurrence()
    subject, body = r.weekly_report(occurrence)
    print(subject)
    print("-" * len(subject))
    print(body)

    cfg = r.ops.weekly_report
    print(
        f"\nSchedule: {cfg.weekday.capitalize()} {cfg.hour:02d}:{cfg.minute:02d} "
        f"{cfg.timezone} ({'enabled' if cfg.enabled else 'DISABLED'})"
    )
    last = r.weekly.last_sent()
    print(f"Last sent: {last.isoformat() if last else 'never'}")
    print(f"Next due:  {r.weekly.next_occurrence().isoformat()}")

    if args.send:
        if r.alerter.notify(subject, body, critical=False):
            r.weekly.mark_sent(occurrence)
            print("\nReport dispatched to configured alert channels.")
        else:
            print("\nReport delivery failed; state not updated (retry with --send).")
            return 1
    return 0


def _cmd_shadow_report(args: argparse.Namespace) -> int:
    """Build the advisory report from local config and persisted data only."""
    from datetime import UTC, datetime, timedelta

    from .config import (
        get_ops,
        get_settings,
        get_signals,
        get_sources,
        get_universe,
    )
    from .llm import get_llm
    from .ops import Alerter
    from .ops.shadow_report import build_shadow_report
    from .store import Store

    settings = get_settings()
    report_cfg = get_ops().shadow_report
    days = args.days if args.days is not None else report_cfg.report_days
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    store = Store(settings.database_url)
    store.init_db()
    subject, body = build_shadow_report(
        store,
        report_cfg,
        get_sources(),
        get_signals(),
        get_universe(),
        get_llm(),
        start,
        end,
    )
    print(subject)
    print("-" * len(subject))
    print(body)
    if args.send and not Alerter(settings).notify(subject, body, critical=False):
        print("\nReport delivery failed.")
        return 1
    if args.send:
        print("\nReport dispatched to configured alert channels.")
    return 0


def _cmd_preview(_args: argparse.Namespace) -> int:
    """Show the exit levels and position size each symbol would get right now.

    Reads live volatility without placing anything, so you can sanity-check that
    targets fit each asset before trusting a soak.
    """
    from .run import Runner
    from .trader.signals import TradeCandidate

    r = Runner()
    if r.market is None:
        print("Market data is disabled; nothing to preview.")
        return 1

    ok, detail = r.market.regime_ok()
    print(f"Regime: {'RISK-ON' if ok else 'RISK-OFF'} ({detail})\n")

    names = [st.name for st in r.strategies]
    header = f"{'SYM':<6}{'TIER':<7}{'MODE':<8}{'ATR%/h':>8}  "
    header += "".join(f"{n.upper() + ' tp/sl':<22}" for n in names)
    header += f"{'SIZE$':>9}"
    print(header)
    print("-" * len(header))

    for ticker, spec in r.universe.symbols.items():
        tier = r.universe.tier_of(ticker, r.signals.default_tier)
        snap = r.market.snapshot(
            spec.product_id,
            sma_periods=r.market_cfg.confirmation.sma_periods,
            lookback_periods=max(1, r.risk.confirm_lookback_hours),
        )
        if not snap.ok:
            print(f"{ticker:<6}{tier:<7}UNAVAILABLE: {snap.detail}")
            continue

        cand = TradeCandidate(
            ticker, spec.product_id, 0.0, 0, 1, "preview", tier=tier, atr_pct=snap.atr_pct
        )
        cells = ""
        for st in r.strategies:
            tp, sl, _ = r.manager.exit_levels(100.0, cand, st)
            cells += f"{f'+{tp - 100:.2f}% / -{100 - sl:.2f}%':<22}"

        first = r.strategies[0]
        notional, _ = r.risk_gate.size_position(
            cand, first, r.manager.allocation_equity(first)
        )
        print(
            f"{ticker:<6}{tier:<7}{r.signals.tier(tier).signal_mode:<8}"
            f"{snap.atr_pct * 100:>7.2f}%  {cells}{notional:>9.2f}"
        )
    print(f"\nSIZE$ is for the '{r.strategies[0].name}' strategy at current allocation equity.")
    return 0


def _cmd_soak_reset(args: argparse.Namespace) -> int:
    """Restart the soak clock after changing entry or exit logic."""
    from pathlib import Path

    from .config import get_ops, get_security
    from .ops.soak import SoakTracker

    tracker = SoakTracker(Path(get_ops().soak.state_file))
    min_days = get_security().min_paper_soak_days
    print(f"Current: {tracker.summary_line(min_days)}")

    if not args.yes:
        answer = input(f"Reset the soak clock to zero ({min_days}d required)? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    state = tracker.restart("paper")
    print(f"Soak clock reset. New start: {state.started_at.isoformat()}")
    return 0


def _cmd_simulate(args: argparse.Namespace) -> int:
    """Deterministic end-to-end demo exercising BOTH strategies.

    Seeds one ticker with baseline + multi-source burst so intraday AND swing
    each open their own independent position, then forces the price to each
    take-profit and closes them. Proves ingest -> score -> per-strategy signal
    -> per-strategy risk/allocation -> paper fill -> exit, with no credentials.
    """
    from .demo import seed_momentum
    from .run import Runner

    # Offline so the demo stays deterministic and credential-free: real price
    # gates would make the outcome depend on live market conditions.
    r = Runner(offline=True)
    if r.broker.name != "paper":
        print("simulate requires PAPER mode (unset LIVE).")
        return 2

    ticker = args.ticker
    if ticker not in r.universe.symbols:
        print(f"Unknown ticker {ticker}; choose from {list(r.universe.symbols)}")
        return 2
    product_id = r.universe.symbols[ticker].product_id

    # 1) Seed data that clears every enabled strategy's thresholds.
    seed_momentum(r.store, ticker, r.strategies)

    # 2) Per-strategy scores.
    for st in r.strategies:
        res = r.scorers[st.name].score_ticker(ticker)
        print(
            f"[{st.name}] {ticker} z={res.zscore:.2f} sources={res.distinct_sources} "
            f"mentions={res.mentions_window} (min_z={st.signal_min_zscore} "
            f"min_src={st.signal_min_distinct_sources} min_men={st.signal_min_mentions})"
        )

    # 3) Evaluate + open, independently per strategy.
    r.evaluate_and_trade()
    opened = {st.name: r.store.open_trade_for(ticker, st.name) for st in r.strategies}
    for name, tr in opened.items():
        if tr is None:
            print(f"WARNING: {name} did not open a position for {ticker}.")
        else:
            print(
                f"OPENED[{name}] {ticker}: qty={tr.qty:.8f} entry={tr.entry_price:.6f} "
                f"tp={tr.take_profit:.6f} sl={tr.stop_loss:.6f}"
            )

    # 4) Force price above this ticker's highest TP so its positions all exit.
    #    Scoped to the product: other symbols' levels must not move it.
    open_trades = [t for t in r.store.open_trades() if t.product_id == product_id]
    if open_trades:
        highest_tp = max(t.take_profit for t in open_trades)
        r.broker.set_price(product_id, highest_tp * 1.05)  # type: ignore[attr-defined]
        r.manager.manage_open_trades()

    # 5) Report closes per strategy.
    for st in r.strategies:
        closed = r.store.closed_trades_for(ticker, st.name)
        if closed:
            t = closed[-1]
            print(
                f"CLOSED[{st.name}] {ticker}: reason={t.exit_reason.value} "
                f"exit={t.exit_price:.6f} pnl=${t.realized_pnl:.2f}"
            )

    print("\nComparison after simulation:")
    _cmd_compare(args)
    print("\nSimulation complete: BOTH strategies exercised end-to-end.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smt", description="Social Momentum Trader")
    p.add_argument("--version", action="version", version=f"smt {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Run the 24/7 trading loop").set_defaults(func=_cmd_run)
    sub.add_parser("init-db", help="Create database tables").set_defaults(func=_cmd_init_db)
    sub.add_parser("score", help="Ingest once and print current momentum scores").set_defaults(
        func=_cmd_score
    )
    sub.add_parser("status", help="Show open positions and PnL by strategy").set_defaults(
        func=_cmd_status
    )
    sub.add_parser("compare", help="Compare strategy performance side by side").set_defaults(
        func=_cmd_compare
    )

    k = sub.add_parser("kill", help="Trip the kill switch")
    k.add_argument("--reason", default="")
    k.set_defaults(func=_cmd_kill)

    sub.add_parser("clear-kill", help="Clear the kill switch").set_defaults(func=_cmd_clear_kill)

    doc = sub.add_parser("doctor", help="Run deployment / go-live preflight checks")
    doc.add_argument("--dev", action="store_true", help="Minimal dev checks only")
    doc.add_argument("--live", action="store_true", help="Include Coinbase go-live checks")
    doc.set_defaults(func=_cmd_doctor)

    sub.add_parser("test-alerts", help="Send a test alert to configured channels").set_defaults(
        func=_cmd_test_alerts
    )
    sub.add_parser(
        "soak-report", help="Paper soak progress + strategy comparison"
    ).set_defaults(func=_cmd_soak_report)

    sub.add_parser(
        "preview", help="Show live exit levels and position sizes per symbol"
    ).set_defaults(func=_cmd_preview)

    weekly = sub.add_parser("weekly-report", help="Preview this week's performance report")
    weekly.add_argument(
        "--send", action="store_true", help="Also deliver it to the alert channels now"
    )
    weekly.add_argument(
        "--last", action="store_true", help="Show the last completed week instead"
    )
    weekly.set_defaults(func=_cmd_weekly_report)

    shadow = sub.add_parser(
        "shadow-report", help="Assess social and Sonnet shadow readiness"
    )
    shadow.add_argument(
        "--days",
        type=_positive_int,
        default=None,
        help="Trailing UTC days (default: config/ops.yaml)",
    )
    shadow.add_argument(
        "--send", action="store_true", help="Also deliver through configured alerts"
    )
    shadow.set_defaults(func=_cmd_shadow_report)

    reset = sub.add_parser(
        "soak-reset", help="Restart the paper soak clock (after a signal change)"
    )
    reset.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    reset.set_defaults(func=_cmd_soak_reset)

    sim = sub.add_parser("simulate", help="Deterministic end-to-end paper demo")
    sim.add_argument("--ticker", default="SOL")
    sim.set_defaults(func=_cmd_simulate)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

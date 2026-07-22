"""Command-line interface: run, status, score, simulate, kill switch controls."""

from __future__ import annotations

import argparse

from . import __version__
from .logging_setup import get_logger

log = get_logger("smt.cli")


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
    from .run import Runner

    r = Runner()
    r.ingest()
    print(f"{'TICKER':<8}{'ZSCORE':>10}{'RECENT':>10}{'BASE':>10}{'MENTIONS':>10}{'SRC':>6}")
    for s in r.scorer.score_all():
        print(
            f"{s.ticker:<8}{s.zscore:>10.2f}{s.recent:>10.1f}"
            f"{s.baseline_mean:>10.1f}{s.mentions_window:>10d}{s.distinct_sources:>6d}"
        )
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    from .run import Runner

    r = Runner()
    print(f"Mode  : {'LIVE' if r.settings.live else 'PAPER'}")
    print(f"Total equity : ${r.manager.equity():.2f}")
    for st in r.strategies:
        open_trades = r.store.open_trades(st.name)
        alloc_eq = r.manager.allocation_equity(st)
        print(
            f"\n[{st.name}] allocation={st.allocation:.0%} "
            f"alloc_equity=${alloc_eq:.2f} open={len(open_trades)}"
        )
        for t in open_trades:
            print(
                f"  - {t.ticker:<6} qty={t.qty:.6f} entry={t.entry_price:.6f} "
                f"tp={t.take_profit:.6f} sl={t.stop_loss:.6f}"
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


def _cmd_simulate(args: argparse.Namespace) -> int:
    """Deterministic end-to-end demo exercising BOTH strategies.

    Seeds one ticker with baseline + multi-source burst so intraday AND swing
    each open their own independent position, then forces the price to each
    take-profit and closes them. Proves ingest -> score -> per-strategy signal
    -> per-strategy risk/allocation -> paper fill -> exit, with no credentials.
    """
    from .demo import seed_momentum
    from .run import Runner

    r = Runner()
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

    # 4) Force price above the highest TP so every open position hits take-profit.
    open_trades = r.store.open_trades()
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

    sim = sub.add_parser("simulate", help="Deterministic end-to-end paper demo")
    sim.add_argument("--ticker", default="SOL")
    sim.set_defaults(func=_cmd_simulate)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

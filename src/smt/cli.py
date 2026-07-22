"""Command-line interface: run, status, score, simulate, kill switch controls."""

from __future__ import annotations

import argparse
import uuid
from datetime import timedelta

from . import __version__
from .logging_setup import get_logger
from .models import SocialEvent, utcnow

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
    from .config import get_settings
    from .store import Store

    settings = get_settings()
    store = Store(settings.database_url)
    store.init_db()
    open_trades = store.open_trades()
    realized = store.total_realized_pnl()
    day_pnl = store.realized_pnl_since(utcnow() - timedelta(days=1))

    print(f"Mode           : {'LIVE' if settings.live else 'PAPER'}")
    print(f"Open positions : {len(open_trades)}")
    for t in open_trades:
        print(
            f"  - {t.ticker:<6} qty={t.qty:.6f} entry={t.entry_price:.6f} "
            f"tp={t.take_profit:.6f} sl={t.stop_loss:.6f}"
        )
    print(f"Realized PnL   : ${realized:.2f} (24h: ${day_pnl:.2f})")
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
    """Deterministic end-to-end demo: seed baseline + burst, open, then hit TP.

    Proves ingest -> score -> signal -> risk -> paper fill -> exit without any
    external credentials or waiting for real time to pass.
    """
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
    bucket = r.risk.scorer_bucket_minutes
    lookback = r.risk.scorer_lookback_buckets

    # 1) Seed a low, steady baseline across older buckets (single source).
    baseline_events: list[SocialEvent] = []
    for i in range(lookback, 1, -1):
        ts = utcnow() - timedelta(minutes=bucket * i - 1)
        for _ in range(2):
            baseline_events.append(
                SocialEvent(
                    source="reddit",
                    external_id=uuid.uuid4().hex,
                    ticker=ticker,
                    author="baseline",
                    text=f"${ticker} chatter",
                    url="",
                    weight=1.0,
                    created_at=ts,
                )
            )
    r.store.add_events(baseline_events)

    # 2) Burst in the most recent bucket, across TWO distinct sources (confirmation).
    burst: list[SocialEvent] = []
    now = utcnow()
    for src in ("reddit", "youtube"):
        for _ in range(12):
            burst.append(
                SocialEvent(
                    source=src,
                    external_id=uuid.uuid4().hex,
                    ticker=ticker,
                    author=f"{src}user",
                    text=f"${ticker} exploding, huge momentum",
                    url="",
                    weight=1.0,
                    created_at=now,
                )
            )
    r.store.add_events(burst)

    # 3) Score + show.
    result = r.scorer.score_ticker(ticker)
    print(
        f"Score for {ticker}: {result.reason} | "
        f"sources={result.distinct_sources} mentions={result.mentions_window}"
    )

    # 4) Evaluate + open.
    r.evaluate_and_trade()
    open_trade = r.store.open_trade_for(ticker)
    if open_trade is None:
        print("No position opened (thresholds not met). Adjust config/risk.yaml.")
        return 1
    print(
        f"OPENED {ticker}: qty={open_trade.qty:.8f} entry={open_trade.entry_price:.6f} "
        f"tp={open_trade.take_profit:.6f} sl={open_trade.stop_loss:.6f}"
    )

    # 5) Force price above TP and manage exits -> should close as TAKE_PROFIT.
    r.broker.set_price(product_id, open_trade.take_profit * 1.05)  # type: ignore[attr-defined]
    r.manager.manage_open_trades()

    closed = r.store.closed_trades_for(ticker)
    if closed:
        t = closed[-1]
        print(
            f"CLOSED {ticker}: reason={t.exit_reason.value} "
            f"exit={t.exit_price:.6f} pnl=${t.realized_pnl:.2f}"
        )
    print(f"Total realized PnL: ${r.store.total_realized_pnl():.2f}")
    print("Simulation complete: ingest -> score -> signal -> risk -> fill -> exit all exercised.")
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
    sub.add_parser("status", help="Show open positions and PnL").set_defaults(func=_cmd_status)

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

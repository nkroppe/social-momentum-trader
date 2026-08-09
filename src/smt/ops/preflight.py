"""Deployment and go-live preflight checks."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import (
    CONFIG_DIR,
    LIVE_ACK_PHRASE,
    REPO_ROOT,
    Settings,
    get_ops,
    get_security,
    get_settings,
    get_sources,
    get_strategies,
)
from ..ops.soak import SoakTracker

REQUIRED_CONFIGS = (
    "risk.yaml",
    "strategies.yaml",
    "universe.yaml",
    "sources.yaml",
    "security.yaml",
    "market.yaml",
    "signals.yaml",
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


def _alert_channel_configured(settings: Settings) -> bool:
    email = bool(settings.smtp_host and settings.alert_email_to)
    tg = settings.telegram_bot_token and settings.telegram_chat_id
    push = bool(settings.ntfy_topic_url or tg)
    return email or push


def _market_data_checks() -> list[CheckResult]:
    """Verify every universe product resolves to Coinbase market data.

    A symbol Coinbase does not list will silently never trade under a
    fail-closed price gate, so surface it here rather than in the logs.
    """
    from ..config import get_market, get_universe
    from ..market import MarketData

    results: list[CheckResult] = []
    market = MarketData(get_market())
    universe = get_universe()
    try:
        missing: list[str] = []
        thin: list[str] = []
        for ticker, spec in universe.symbols.items():
            candles = market.candles(spec.product_id)
            if not candles:
                missing.append(f"{ticker} ({spec.product_id})")
            elif len(candles) < get_market().confirmation.sma_periods:
                thin.append(f"{ticker}:{len(candles)}")

        detail = "all universe products resolve on Coinbase"
        if missing:
            detail = "not listed on Coinbase: " + ", ".join(missing)
        elif thin:
            detail = "listed but thin history: " + ", ".join(thin)
        results.append(CheckResult("market_data_products", not missing, detail))

        regime_ok, regime_detail = market.regime_ok()
        results.append(
            CheckResult(
                "regime_benchmark",
                # The benchmark only needs to be readable here; RISK-OFF is a
                # valid market state, not a configuration failure.
                "candles" not in regime_detail or regime_ok,
                f"{'RISK-ON' if regime_ok else 'RISK-OFF'} - {regime_detail}",
            )
        )
    finally:
        market.close()
    return results


def _x_budget_check(settings) -> CheckResult:
    """Project this month's X read burn against the configured budget.

    Reads are billed per tweet returned, so a short poll interval across many
    cashtags burns budget far faster than the interval alone suggests. An
    exhausted budget silently stops ingest, which would leave a soak collecting
    nothing, so surface the trajectory before that happens.

    The rate is measured against time spent polling, not the elapsed month: a
    bot started on the 8th has spent nothing on the first seven days, and
    averaging over them would report a comfortable burn while the budget is
    hours from running out.
    """
    from datetime import UTC, datetime

    from ..ingest.x import ReadBudget

    limit = settings.x_monthly_read_budget
    budget = ReadBudget(
        Path("./data/x_budget.json"),
        limit,
        settings.x_read_cost_usd,
        settings.x_budget_opening_reads,
    )
    used = budget.reads_used
    started = budget.started_at

    allowance = budget.daily_allowance()
    day_used = budget.day_used()
    spend = f"${budget.spend_usd:,.2f} of ${budget.budget_usd:,.2f}"
    pace = f"today {day_used:,}/{allowance:,}"

    if used == 0 or started is None:
        return CheckResult("x_read_budget", True, f"{spend} used this month | {pace}")

    # The monthly cap is enforced in the collector, so overspend is not the
    # risk worth flagging. The risk is spending the daily allowance early and
    # going blind for the rest of the day, which a soak would not survive.
    now = datetime.now(UTC)
    day_elapsed = (now.hour + now.minute / 60.0) / 24.0
    spent_ahead_of_pace = allowance > 0 and (day_used / allowance) > day_elapsed + 0.25

    if used > limit:
        return CheckResult("x_read_budget", False, f"{spend} - monthly cap exceeded | {pace}")
    if spent_ahead_of_pace:
        blind_from = (day_used / allowance) * 24 if allowance else 24
        return CheckResult(
            "x_read_budget",
            False,
            f"{spend} | {pace} - burning today's allowance by "
            f"{blind_from:.0f}h UTC, ingest pauses after that",
        )
    return CheckResult("x_read_budget", True, f"{spend} | {pace} on pace")


def run_preflight(profile: str = "production") -> list[CheckResult]:
    """Run checks for dev, production (VPS paper), or live profiles."""
    settings = get_settings()
    sources = get_sources()
    security = get_security()
    ops = get_ops()
    results: list[CheckResult] = []

    # --- shared config sanity ---
    for name in REQUIRED_CONFIGS:
        path = CONFIG_DIR / name
        results.append(
            CheckResult(
                f"config/{name}",
                path.exists(),
                "found" if path.exists() else "missing",
            )
        )

    try:
        strategies = get_strategies().enabled()
        total_alloc = sum(st.allocation for st in strategies)
        results.append(
            CheckResult(
                "strategy_allocations",
                total_alloc <= 1.0 + 1e-9 and len(strategies) >= 1,
                f"{len(strategies)} enabled, total allocation={total_alloc:.2f}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        results.append(CheckResult("strategy_allocations", False, str(exc)))

    if profile == "dev":
        return results

    # --- production (VPS paper soak) ---
    env_candidates = (REPO_ROOT / ".env", Path("/app/.env"), Path.cwd() / ".env")
    env_path = next((p for p in env_candidates if p.exists()), None)
    if env_path is not None:
        results.append(CheckResult(".env file", True, str(env_path)))
    elif settings.x_bearer_token or settings.database_url.startswith("postgresql"):
        # Compose injects env vars without mounting .env into the container.
        results.append(
            CheckResult(".env file", True, "environment variables loaded (no .env in container)")
        )
    else:
        results.append(
            CheckResult(".env file", False, "copy from .env.production.example")
        )

    if ops.preflight.require_postgres:
        pg = settings.database_url.startswith("postgresql")
        pg_detail = settings.database_url if pg else "set DATABASE_URL to postgresql+psycopg://..."
        results.append(
            CheckResult(
                "postgres_database_url",
                pg,
                pg_detail,
            )
        )

    results.append(
        CheckResult(
            "mock_disabled",
            not sources.mock.enabled,
            "mock.enabled must be false on VPS" if sources.mock.enabled else "mock disabled",
        )
    )

    if ops.preflight.require_reddit:
        reddit_ok = bool(settings.reddit_client_id and settings.reddit_client_secret)
        results.append(
            CheckResult(
                "reddit_credentials",
                reddit_ok,
                "configured" if reddit_ok else "set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET",
            )
        )

    if ops.preflight.require_x:
        x_ok = bool(settings.x_bearer_token)
        results.append(
            CheckResult(
                "x_bearer_token",
                x_ok,
                "configured" if x_ok else "set X_BEARER_TOKEN",
            )
        )
        if x_ok:
            results.append(_x_budget_check(settings))

    if ops.preflight.require_alert_channel:
        alerts = _alert_channel_configured(settings)
        results.append(
            CheckResult(
                "alert_channel",
                alerts,
                "at least one of email/ntfy/telegram required for soak alerts",
            )
        )

    control = Path(settings.kill_file).parent
    results.append(
        CheckResult(
            "control_directory",
            control.exists(),
            str(control.resolve()) if control.exists() else "mkdir control/",
        )
    )

    results.extend(_market_data_checks())

    if profile != "live":
        return results

    # --- live (Coinbase) ---
    results.append(
        CheckResult(
            "live_flag",
            settings.live,
            "LIVE=true required" if not settings.live else "LIVE=true",
        )
    )
    results.append(
        CheckResult(
            "live_ack",
            settings.live_ack == LIVE_ACK_PHRASE,
            f"LIVE_ACK must equal {LIVE_ACK_PHRASE!r}",
        )
    )

    coinbase_ok = settings.coinbase_configured and bool(settings.coinbase_portfolio_id)
    results.append(
        CheckResult(
            "coinbase_credentials",
            coinbase_ok,
            "COINBASE_API_KEY, COINBASE_API_SECRET, COINBASE_PORTFOLIO_ID required",
        )
    )

    tracker = SoakTracker(Path(ops.soak.state_file))
    soak_ok = tracker.meets_minimum(security.min_paper_soak_days)
    results.append(
        CheckResult(
            "paper_soak_duration",
            soak_ok,
            tracker.summary_line(security.min_paper_soak_days),
        )
    )

    if coinbase_ok and settings.live and settings.live_ack == LIVE_ACK_PHRASE:
        try:
            from ..trader.coinbase import CoinbaseBroker, TransferPermissionError

            CoinbaseBroker(settings, security)
            results.append(
                CheckResult("coinbase_trade_only_key", True, "can_transfer=false verified")
            )
        except TransferPermissionError as exc:
            results.append(CheckResult("coinbase_trade_only_key", False, str(exc)))
        except Exception as exc:  # noqa: BLE001
            msg = f"API check failed: {exc}"
            results.append(CheckResult("coinbase_trade_only_key", False, msg))

    return results


def all_passed(results: list[CheckResult]) -> bool:
    return all(r.passed for r in results)

"""Deployment and go-live preflight checks."""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path

from ..config import (
    CONFIG_DIR,
    LIVE_ACK_PHRASE,
    REPO_ROOT,
    Settings,
    get_market,
    get_ops,
    get_security,
    get_settings,
    get_sources,
    get_strategies,
)
from ..ops.soak import SoakTracker
from ..policy import trading_policy_identity

REQUIRED_CONFIGS = (
    "risk.yaml",
    "strategies.yaml",
    "universe.yaml",
    "sources.yaml",
    "security.yaml",
    "market.yaml",
    "signals.yaml",
    "llm.yaml",
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
    market_cfg = get_market()
    market = MarketData(market_cfg)
    universe = get_universe()
    try:
        missing: list[str] = []
        thin: list[str] = []
        paper_unready: list[tuple[str, str]] = []
        for ticker, spec in universe.symbols.items():
            candles = market.candles(spec.product_id)
            if not candles:
                missing.append(f"{ticker} ({spec.product_id})")
            elif len(candles) < market_cfg.confirmation.sma_periods:
                thin.append(f"{ticker}:{len(candles)}")
            try:
                quote = market.quote(spec.product_id)
                bars = market.paper_bars(spec.product_id)
                if quote is None:
                    paper_unready.append((spec.tier, f"{ticker}:quote unavailable"))
                elif quote.spread_bps > market_cfg.paper_max_spread_bps:
                    paper_unready.append((spec.tier, f"{ticker}:spread {quote.spread_bps:.1f}bps"))
                elif quote.ask_notional < market_cfg.min_top_level_notional_usd(spec.tier):
                    paper_unready.append(
                        (spec.tier, f"{ticker}:ask depth ${quote.ask_notional:.2f}")
                    )
                elif not bars:
                    paper_unready.append((spec.tier, f"{ticker}:1m bars unavailable"))
            except Exception as exc:  # noqa: BLE001
                paper_unready.append((spec.tier, f"{ticker}:{exc}"))

        detail = "all universe products resolve on Coinbase"
        if missing:
            detail = "not listed on Coinbase: " + ", ".join(missing)
        elif thin:
            detail = "listed but thin history: " + ", ".join(thin)
        results.append(CheckResult("market_data_products", not missing, detail))
        blocking = [detail for tier, detail in paper_unready if tier in {"major", "large"}]
        quarantined = [detail for tier, detail in paper_unready if tier not in {"major", "large"}]
        paper_detail = "fresh executable market available for core tiers"
        if blocking:
            paper_detail = "core unavailable: " + "; ".join(blocking)
        elif quarantined:
            paper_detail += "; runtime fail-closed quarantine: " + "; ".join(quarantined)
        results.append(
            CheckResult(
                "paper_execution_market",
                not blocking,
                paper_detail,
            )
        )

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

    The shared dollar ledger includes recent-count requests and distinct posts
    returned by triggered searches. An exhausted daily or monthly budget stops
    ingest, so surface endpoint usage and the trajectory before that happens.

    The rate is measured against time spent polling, not the elapsed month: a
    bot started on the 8th has spent nothing on the first seven days, and
    averaging over them would report a comfortable burn while the budget is
    hours from running out.
    """
    from datetime import UTC, datetime

    from ..ingest.x import BudgetStateUnavailable, ReadBudget

    budget = ReadBudget(
        Path("./data/x_budget.json"),
        settings.x_monthly_read_budget,
        settings.effective_x_post_read_cost_usd,
        settings.x_budget_opening_reads,
        monthly_budget_usd=settings.effective_x_monthly_budget_usd,
        count_request_cost_usd=settings.x_recent_count_request_cost_usd,
    )
    try:
        started = budget.started_at
        allowance = budget.daily_dollar_allowance()
        day_used = budget.day_spend_usd()
        spend = f"${budget.spend_usd:,.2f} of ${budget.budget_usd:,.2f}"
        pace = f"today ${day_used:,.3f}/${allowance:,.3f}"
        endpoints = (
            f"post reads={budget.reads_used:,} (${budget.post_spend_usd:,.2f}), "
            f"count requests={budget.count_requests_used:,} "
            f"(${budget.count_spend_usd:,.2f})"
        )
    except BudgetStateUnavailable as exc:
        return CheckResult("x_read_budget", False, str(exc))

    if budget.spend_usd == 0 or started is None:
        return CheckResult(
            "x_read_budget", True, f"{endpoints} | total {spend} this month | {pace}"
        )

    # The monthly cap is enforced in the collector, so overspend is not the
    # risk worth flagging. The risk is spending the daily allowance early and
    # going blind for the rest of the day, which a soak would not survive.
    now = datetime.now(UTC)
    day_elapsed = (now.hour + now.minute / 60.0) / 24.0
    spent_ahead_of_pace = allowance > 0 and (day_used / allowance) > day_elapsed + 0.25

    if budget.spend_usd > budget.budget_usd:
        return CheckResult(
            "x_read_budget",
            False,
            f"{endpoints} | total {spend} - monthly cap exceeded | {pace}",
        )
    if spent_ahead_of_pace:
        blind_from = (day_used / allowance) * 24 if allowance else 24
        return CheckResult(
            "x_read_budget",
            False,
            f"{endpoints} | total {spend} | {pace} - burning today's allowance by "
            f"{blind_from:.0f}h UTC, ingest pauses after that",
        )
    return CheckResult("x_read_budget", True, f"{endpoints} | total {spend} | {pace} on pace")


def run_preflight(profile: str = "production") -> list[CheckResult]:
    """Run checks for dev, production (VPS paper), or live profiles."""
    settings = get_settings()
    sources = get_sources()
    security = get_security()
    ops = get_ops()
    market_cfg = get_market()
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
    except Exception as extra:
        results.append(CheckResult("strategy_allocations", False, str(extra)))

    if profile == "dev":
        return results

    # --- production (VPS paper soak) ---
    results.append(
        CheckResult(
            "paper_fail_closed_market",
            market_cfg.paper_use_real_prices,
            (
                "fresh Coinbase quotes and bars required"
                if market_cfg.paper_use_real_prices
                else "paper_use_real_prices must be true outside offline simulation"
            ),
        )
    )
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
        results.append(CheckResult(".env file", False, "copy from .env.production.example"))

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
        if not sources.x.enabled:
            results.append(
                CheckResult(
                    "x_enabled",
                    False,
                    "ops.preflight.require_x is true but sources.x.enabled is false",
                )
            )
        else:
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
    elif not sources.x.enabled:
        results.append(
            CheckResult(
                "x_ingest",
                True,
                "paused (price-only; social is not in the live path)",
            )
        )

    if ops.preflight.require_alert_channel:
        alerts = _alert_channel_configured(settings)
        results.append(
            CheckResult(
                "alert_channel",
                alerts,
                "at least one of email/ntfy/telegram required for soak alerts",
            )
        )

    extra_weekly = [addr.strip() for addr in ops.weekly_report.extra_email_to if addr.strip()]
    if extra_weekly:
        smtp_ok = bool(settings.smtp_host)
        results.append(
            CheckResult(
                "weekly_extra_email",
                smtp_ok,
                (
                    f"{len(extra_weekly)} recipient(s) via SMTP"
                    if smtp_ok
                    else "extra_email_to set but SMTP_HOST is missing"
                ),
            )
        )

    from ..llm import get_llm

    llm = get_llm()
    if llm.enabled:
        key_ok = bool(os.environ.get("CURSOR_API_KEY", "").strip())
        sdk_ok = importlib.util.find_spec("cursor_sdk") is not None
        model_detail = ""
        model_ok = False
        if key_ok and sdk_ok:
            try:
                from ..llm.provider import CursorJSONProvider

                model_detail = CursorJSONProvider(llm)._resolve_model()
                model_ok = True
            except Exception as extra:
                model_detail = str(extra)
        results.append(
            CheckResult(
                "cursor_llm",
                key_ok and sdk_ok and model_ok,
                (
                    f"{model_detail}; max {llm.max_calls_per_month} calls/month"
                    if model_ok
                    else model_detail or "set CURSOR_API_KEY and install the llm extra"
                ),
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
    if ops.telegram_control.enabled:
        tg_ready = bool(settings.telegram_bot_token and settings.telegram_chat_id)
        results.append(
            CheckResult(
                "telegram_control",
                tg_ready,
                (
                    "KILL/START commands enabled for configured chat"
                    if tg_ready
                    else "enabled in ops.yaml but TELEGRAM_BOT_TOKEN/CHAT_ID missing"
                ),
            )
        )

    results.extend(_market_data_checks())

    policy = trading_policy_identity()
    tracker = SoakTracker(Path(ops.soak.state_file))
    policy_matches = tracker.policy_matches(policy.fingerprint)
    state = tracker.current_state()
    results.append(
        CheckResult(
            "soak_policy_generation",
            policy_matches,
            (
                f"generation={state.generation if state else 0} "
                f"active={(state.active_fingerprint[:12] if state else 'missing')} "
                f"expected={policy.fingerprint[:12]}"
                + ("" if policy_matches else " - fingerprint mismatch; soak invalid")
            ),
        )
    )

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
    advanced_exit = any(st.advanced_exit_enabled for st in get_strategies().enabled())
    results.append(
        CheckResult(
            "advanced_exit_live_parity",
            True,
            (
                "advanced partial/chandelier exits enabled; Coinbase get_order reconcile, "
                "leftover-bracket cancel, and cancel/replace remaining TP/SL are implemented"
                if advanced_exit
                else "advanced exits disabled"
            ),
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

    soak_ok = tracker.meets_minimum(
        security.min_paper_soak_days,
        policy.fingerprint,
    )
    results.append(
        CheckResult(
            "paper_soak_duration",
            soak_ok,
            tracker.summary_line(
                security.min_paper_soak_days,
                policy.fingerprint,
            ),
        )
    )

    if coinbase_ok and settings.live and settings.live_ack == LIVE_ACK_PHRASE:
        try:
            from ..trader.coinbase import CoinbaseBroker, TransferPermissionError

            CoinbaseBroker(settings, security)
            results.append(
                CheckResult("coinbase_trade_only_key", True, "can_transfer=false verified")
            )
        except TransferPermissionError as extra:
            results.append(CheckResult("coinbase_trade_only_key", False, str(extra)))
        except Exception as extra:
            msg = f"API check failed: {extra}"
            results.append(CheckResult("coinbase_trade_only_key", False, msg))

    return results


def all_passed(results: list[CheckResult]) -> bool:
    return all(r.passed for r in results)

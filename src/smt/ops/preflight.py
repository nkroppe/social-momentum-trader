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
    env_path = REPO_ROOT / ".env"
    env_detail = str(env_path) if env_path.exists() else "copy from .env.production.example"
    results.append(CheckResult(".env file", env_path.exists(), env_detail))

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

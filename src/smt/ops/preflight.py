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
            except Exception as extra:
                paper_unready.append((spec.tier, f"{ticker}:{extra}"))

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

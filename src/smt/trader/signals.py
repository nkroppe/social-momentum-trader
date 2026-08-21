"""Price-action-first signal engine with tier-specific social playbooks.

Production candidates always begin with a deterministic multi-timeframe setup.
Bull strategies use EMA 9/21/50 alignment, RSI, and breakout/retest/VWAP
patterns. The bear_rally strategy uses RISK-OFF-only relief setups (RSI reclaim,
failed breakdown, relative-strength bounce) on an allowlisted universe.
Social is then ignored, optional, required, or catalyst-required by liquidity
tier; it can never create a candidate without the price setup.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..config import (
    ConfirmationConfig,
    EntryRulesConfig,
    MarketConfig,
    SignalsConfig,
    StrategyConfig,
    TierConfig,
    UniverseConfig,
    get_market,
    get_signals,
)
from ..logging_setup import get_logger
from ..market import (
    Candle,
    MarketData,
    TechnicalSnapshot,
    atr,
    ema,
    relative_volume,
    rolling_vwap,
    rsi,
    structure_levels,
    volatility_compression,
)
from ..scorer import ScoreResult

log = get_logger("smt.signals")


@dataclass(frozen=True)
class GateResult:
    """Outcome of one entry gate.

    `evaluated` is False when the gate could not run at all (no market data, or
    the gate is switched off). A mode that depends on a gate must not trade when
    that gate never ran, regardless of the `passed` default.
    """

    passed: bool
    why: str
    evaluated: bool = True


@dataclass
class TradeCandidate:
    ticker: str
    product_id: str
    zscore: float
    mentions: int
    sources: int
    reason: str
    strategy: str = "intraday"
    authors: int = 0
    bullish_ratio: float = 0.0
    tier: str = "mid"
    atr_pct: float = 0.0
    trailing_return: float = 0.0
    setup: str = ""
    entry_price: float = 0.0
    structure_stop: float = 0.0
    stop_pct: float = 0.0
    conviction: float = 1.0
    size_multiplier: float = 1.0
    setup_metadata: dict[str, str | float | bool] = field(default_factory=dict)
    count_volume: int = 0
    engagement: int = 0
    decision_mode: str = "shadow"
    social_decision: str = ""
    social_reason: str = ""
    llm_status: str = ""
    llm_score: float = 0.0
    llm_veto: bool = False
    llm_reason: str = ""
    opportunity_key: str = ""

    @property
    def decision_key(self) -> str:
        setup_id = self.setup_metadata.get("trigger_ts") or (
            f"{self.setup}:{float(self.setup_metadata.get('breakout_level', 0.0)):.8f}"
        )
        stable = f"{self.ticker}|{self.strategy}|{self.tier}|{setup_id}"
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class PriceSetup:
    name: str
    entry_price: float
    structure_stop: float
    stop_pct: float
    atr_pct: float
    conviction: float
    metadata: dict[str, str | float | bool]


@dataclass(frozen=True)
class _PriceSetupResult:
    setup: PriceSetup | None
    evaluated: bool
    detail: str
    trigger_ts: int = 0
    trigger_close: float = 0.0
    features: dict[str, str | float | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalEvaluation:
    """Prospective outcome for one symbol at one closed trigger candle."""

    ticker: str
    product_id: str
    strategy: str
    tier: str
    trigger_granularity_seconds: int
    trigger_candle_ts: int
    trigger_closed_at: datetime
    outcome_status: str
    outcome_reason: str
    regime_status: str
    regime_reason: str
    price_status: str
    price_reason: str
    setup_status: str
    setup_name: str
    setup_reason: str
    confirmation_status: str
    confirmation_reason: str
    social_status: str
    social_reason: str
    feature_snapshot: dict[str, str | float | bool]
    candidate: TradeCandidate | None = None


_SETUP_QUALITY = {
    "breakout_retest": 3,
    "failed_breakdown": 3,
    "rsi_reclaim": 2,
    "vwap_pullback": 2,
    "rs_bounce": 2,
    "breakout_close": 1,
}

_SETUP_CONVICTION = {
    "breakout_close": 0.85,
    "breakout_retest": 1.0,
    "vwap_pullback": 0.90,
    "rsi_reclaim": 0.90,
    "failed_breakdown": 0.95,
    "rs_bounce": 0.85,
}


def _candidate_rank(candidate: TradeCandidate) -> tuple[float | str, ...]:
    """Rank only on deterministic price evidence; social fields are audit-only."""
    price_conviction = float(candidate.setup_metadata.get("price_conviction", 0.0))
    relative_volume = float(candidate.setup_metadata.get("relative_volume", 0.0))
    return (
        -_SETUP_QUALITY.get(candidate.setup, 0),
        -price_conviction,
        -relative_volume,
        candidate.ticker,
    )


def _ema_stack(candles: list[Candle]) -> tuple[bool, tuple[float, float, float]]:
    values = (ema(candles, 9), ema(candles, 21), ema(candles, 50))
    return all(values) and values[0] > values[1] > values[2], values


def _bearish_ema_stack(values: tuple[float, float, float]) -> bool:
    return all(values) and values[0] < values[1] < values[2]


def _retest_setup(
    candles: list[Candle], rules: EntryRulesConfig, min_relative_volume: float
) -> tuple[float, float, float, int] | None:
    """Return level, retest low, breakout relative volume, and breakout index."""
    if len(candles) < rules.breakout_lookback + rules.retest_window + 2:
        return None
    latest = candles[-1]
    start = max(rules.breakout_lookback, len(candles) - rules.retest_window - 1)
    for idx in range(len(candles) - 2, start - 1, -1):
        prior = candles[idx - rules.breakout_lookback : idx]
        level = max(c.high for c in prior)
        breakout_slice = candles[: idx + 1]
        breakout = candles[idx]
        if breakout.close <= level:
            continue
        breakout_relative_volume = relative_volume(breakout_slice, rules.volume_lookback)
        if breakout_relative_volume < min_relative_volume:
            continue
        tolerance = level * rules.retest_tolerance_pct
        if latest.low <= level + tolerance and latest.close > level and latest.close >= latest.open:
            retest_low = min(c.low for c in candles[idx:])
            return level, retest_low, breakout_relative_volume, idx
    return None


def _chase_blocked(trigger: list[Candle], rules: EntryRulesConfig) -> bool:
    lookback = min(rules.chase_lookback_bars, len(trigger) - 1)
    if lookback <= 0 or trigger[-1].close <= 0 or trigger[-1 - lookback].close <= 0:
        return False
    chase_ret = trigger[-1].close / trigger[-1 - lookback].close - 1.0
    return chase_ret > rules.max_chase_return_pct


def _finalize_setup(
    *,
    setup_name: str,
    trigger: list[Candle],
    bias: list[Candle],
    rules: EntryRulesConfig,
    strategy_name: str,
    breakout_level: float,
    stop_reference: float,
    rel_vol: float,
    trigger_rsi: float,
    trigger_emas: tuple[float, float, float],
    bias_emas: tuple[float, float, float],
    compressed: bool,
    extra_metadata: dict[str, str | float | bool] | None = None,
) -> PriceSetup | None:
    latest = trigger[-1]
    high, low = structure_levels(trigger, rules.structure_lookback)
    atr_abs = atr(trigger, 14)
    structure_stop = stop_reference - atr_abs * rules.stop_atr_buffer
    stop_pct = (latest.close - structure_stop) / latest.close if latest.close > 0 else 0.0
    if stop_pct < rules.min_stop_pct:
        stop_pct = rules.min_stop_pct
        structure_stop = latest.close * (1.0 - stop_pct)
    if stop_pct > rules.max_stop_pct or structure_stop <= 0 or structure_stop >= latest.close:
        return None

    conviction = _SETUP_CONVICTION.get(setup_name, 0.8)
    if compressed:
        conviction = min(conviction + 0.05, 1.0)
    metadata: dict[str, str | float | bool] = {
        "setup": setup_name,
        "strategy": strategy_name,
        "trigger_ts": str(latest.ts),
        "breakout_level": breakout_level,
        "structure_high": high,
        "structure_low": low,
        "relative_volume": rel_vol,
        "rsi": trigger_rsi,
        "ema9": trigger_emas[0],
        "ema21": trigger_emas[1],
        "ema50": trigger_emas[2],
        "bias_ema9": bias_emas[0],
        "bias_ema21": bias_emas[1],
        "bias_ema50": bias_emas[2],
        "compressed": compressed,
        "price_conviction": conviction,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return PriceSetup(
        name=setup_name,
        entry_price=latest.close,
        structure_stop=structure_stop,
        stop_pct=stop_pct,
        atr_pct=(atr_abs / latest.close) if latest.close > 0 else 0.0,
        conviction=conviction,
        metadata=metadata,
    )


def _detect_bull_breakout_setup(
    trigger: list[Candle],
    bias: list[Candle],
    rules: EntryRulesConfig,
    tier: TierConfig,
    strategy_name: str,
) -> PriceSetup | None:
    trigger_stack, trigger_emas = _ema_stack(trigger)
    bias_stack, bias_emas = _ema_stack(bias)
    if rules.require_trigger_ema_stack and not trigger_stack:
        return None
    if rules.require_bias_ema_stack and not bias_stack:
        return None
    if rules.reject_bearish_bias_stack and _bearish_ema_stack(bias_emas):
        return None

    trigger_rsi = rsi(trigger, rules.rsi_periods)
    if trigger_rsi < rules.rsi_min:
        return None

    compressed = volatility_compression(
        trigger,
        rules.compression_lookback,
        rules.compression_recent,
        rules.compression_ratio_max,
    )
    latest = trigger[-1]
    high, _ = structure_levels(trigger, rules.breakout_lookback)
    _, low = structure_levels(trigger, rules.structure_lookback)
    if high <= 0 or low <= 0 or latest.close <= 0:
        return None

    setup_name = ""
    breakout_level = high
    stop_reference = low
    rel_vol = relative_volume(trigger, rules.volume_lookback)

    retest = _retest_setup(trigger, rules, tier.min_relative_volume)
    if retest is not None:
        setup_name = "breakout_retest"
        breakout_level, stop_reference, rel_vol, breakout_idx = retest
        compressed = volatility_compression(
            trigger[: breakout_idx + 1],
            rules.compression_lookback,
            rules.compression_recent,
            rules.compression_ratio_max,
        )
    elif (
        tier.retest_policy != "required"
        and latest.close > high
        and rel_vol >= tier.min_relative_volume
    ):
        setup_name = "breakout_close"
        stop_reference = min(low, latest.low)
    elif strategy_name == "intraday" and rules.allow_vwap_pullback and tier.allow_vwap_pullback:
        vwap = rolling_vwap(trigger, rules.vwap_periods)
        tolerance = vwap * rules.retest_tolerance_pct
        if (
            vwap > 0
            and latest.low <= vwap + tolerance
            and latest.close > vwap
            and latest.close >= latest.open
            and rel_vol >= tier.min_relative_volume
        ):
            setup_name = "vwap_pullback"
            breakout_level = vwap
            stop_reference = min(c.low for c in trigger[-rules.retest_window :])
    if not setup_name:
        return None
    if rules.require_compression and not compressed:
        return None

    return _finalize_setup(
        setup_name=setup_name,
        trigger=trigger,
        bias=bias,
        rules=rules,
        strategy_name=strategy_name,
        breakout_level=breakout_level,
        stop_reference=stop_reference,
        rel_vol=rel_vol,
        trigger_rsi=trigger_rsi,
        trigger_emas=trigger_emas,
        bias_emas=bias_emas,
        compressed=compressed,
    )


def _rsi_reclaim_setup(
    trigger: list[Candle],
    rules: EntryRulesConfig,
    min_relative_volume: float,
) -> tuple[float, float, float] | None:
    """Return reclaim RSI, stop reference, and relative volume when present."""
    lookback = rules.rsi_lookback_bars
    if len(trigger) < rules.rsi_periods + lookback + 1:
        return None
    latest = trigger[-1]
    if latest.close < latest.open:
        return None
    rel_vol = relative_volume(trigger, rules.volume_lookback)
    if rel_vol < min_relative_volume:
        return None

    rsi_values: list[float] = []
    for end in range(len(trigger) - lookback, len(trigger)):
        rsi_values.append(rsi(trigger[: end + 1], rules.rsi_periods))
    if not rsi_values:
        return None
    prior = rsi_values[:-1]
    if not prior or min(prior) > rules.rsi_oversold_max:
        return None
    if rsi_values[-1] < rules.rsi_reclaim_min:
        return None
    if latest.close <= trigger[-2].high:
        return None
    stop_reference = min(c.low for c in trigger[-lookback:])
    return rsi_values[-1], stop_reference, rel_vol


def _failed_breakdown_setup(
    trigger: list[Candle],
    rules: EntryRulesConfig,
    min_relative_volume: float,
) -> tuple[float, float, float] | None:
    """Return breakdown level, sweep low, and relative volume when reclaimed."""
    needed = rules.failed_breakdown_lookback + rules.retest_window + 2
    if len(trigger) < needed:
        return None
    latest = trigger[-1]
    if latest.close < latest.open:
        return None
    structure = trigger[
        -(rules.failed_breakdown_lookback + rules.retest_window + 1) : -rules.retest_window
    ]
    if len(structure) < 5:
        return None
    level = min(c.low for c in structure)
    if level <= 0:
        return None
    sweep_low = level
    swept = False
    for candle in trigger[-rules.retest_window - 1 : -1]:
        if candle.low < level:
            swept = True
            sweep_low = min(sweep_low, candle.low)
    if not swept:
        return None
    if latest.close <= level:
        return None
    rel_vol = relative_volume(trigger, rules.volume_lookback)
    if rel_vol < min_relative_volume:
        return None
    return level, sweep_low, rel_vol


def _rs_bounce_setup(
    trigger: list[Candle],
    benchmark: list[Candle],
    rules: EntryRulesConfig,
    min_relative_volume: float,
) -> tuple[float, float, float, float] | None:
    """Return RS edge, reclaim level, stop reference, and relative volume."""
    lookback = rules.rs_lookback_bars
    if len(trigger) < lookback + 2 or len(benchmark) < lookback + 2:
        return None
    latest = trigger[-1]
    if latest.close < latest.open:
        return None
    asset_base = trigger[-1 - lookback].close
    bench_base = benchmark[-1 - lookback].close
    if asset_base <= 0 or bench_base <= 0 or benchmark[-1].close <= 0:
        return None
    asset_ret = latest.close / asset_base - 1.0
    bench_ret = benchmark[-1].close / bench_base - 1.0
    rs_edge = asset_ret - bench_ret
    if rs_edge < rules.rs_min_outperformance_pct:
        return None

    rel_vol = relative_volume(trigger, rules.volume_lookback)
    if rel_vol < min_relative_volume:
        return None

    ema9 = ema(trigger, 9)
    vwap = rolling_vwap(trigger, rules.vwap_periods)
    tolerance_ema = ema9 * rules.retest_tolerance_pct
    tolerance_vwap = vwap * rules.retest_tolerance_pct
    reclaim_level = 0.0
    if ema9 > 0 and latest.low <= ema9 + tolerance_ema and latest.close > ema9:
        reclaim_level = ema9
    elif vwap > 0 and latest.low <= vwap + tolerance_vwap and latest.close > vwap:
        reclaim_level = vwap
    else:
        return None
    stop_reference = min(c.low for c in trigger[-rules.retest_window :])
    return rs_edge, reclaim_level, stop_reference, rel_vol


def _detect_bear_rally_setup(
    trigger: list[Candle],
    bias: list[Candle],
    rules: EntryRulesConfig,
    tier: TierConfig,
    strategy_name: str,
    benchmark: list[Candle] | None = None,
) -> PriceSetup | None:
    if _chase_blocked(trigger, rules):
        return None

    _, trigger_emas = _ema_stack(trigger)
    _, bias_emas = _ema_stack(bias)
    trigger_rsi = rsi(trigger, rules.rsi_periods)
    compressed = volatility_compression(
        trigger,
        rules.compression_lookback,
        rules.compression_recent,
        rules.compression_ratio_max,
    )
    min_rvol = tier.min_relative_volume

    if rules.allow_failed_breakdown:
        failed = _failed_breakdown_setup(trigger, rules, min_rvol)
        if failed is not None:
            level, sweep_low, rel_vol = failed
            setup = _finalize_setup(
                setup_name="failed_breakdown",
                trigger=trigger,
                bias=bias,
                rules=rules,
                strategy_name=strategy_name,
                breakout_level=level,
                stop_reference=sweep_low,
                rel_vol=rel_vol,
                trigger_rsi=trigger_rsi,
                trigger_emas=trigger_emas,
                bias_emas=bias_emas,
                compressed=compressed,
            )
            if setup is not None:
                return setup

    if rules.allow_rsi_reclaim:
        reclaim = _rsi_reclaim_setup(trigger, rules, min_rvol)
        if reclaim is not None:
            reclaim_rsi, stop_reference, rel_vol = reclaim
            setup = _finalize_setup(
                setup_name="rsi_reclaim",
                trigger=trigger,
                bias=bias,
                rules=rules,
                strategy_name=strategy_name,
                breakout_level=trigger[-1].close,
                stop_reference=stop_reference,
                rel_vol=rel_vol,
                trigger_rsi=reclaim_rsi,
                trigger_emas=trigger_emas,
                bias_emas=bias_emas,
                compressed=compressed,
                extra_metadata={"rsi_reclaim": reclaim_rsi},
            )
            if setup is not None:
                return setup

    if rules.allow_rs_bounce and benchmark is not None:
        bounce = _rs_bounce_setup(trigger, benchmark, rules, min_rvol)
        if bounce is not None:
            rs_edge, reclaim_level, stop_reference, rel_vol = bounce
            setup = _finalize_setup(
                setup_name="rs_bounce",
                trigger=trigger,
                bias=bias,
                rules=rules,
                strategy_name=strategy_name,
                breakout_level=reclaim_level,
                stop_reference=stop_reference,
                rel_vol=rel_vol,
                trigger_rsi=trigger_rsi,
                trigger_emas=trigger_emas,
                bias_emas=bias_emas,
                compressed=compressed,
                extra_metadata={"rs_edge": rs_edge},
            )
            if setup is not None:
                return setup
    return None


def _setup_reject_diagnostics(
    trigger: list[Candle],
    bias: list[Candle],
    rules: EntryRulesConfig,
) -> dict[str, str | float | bool]:
    """Capture gate proximity features even when no setup qualifies.

    Opportunity-ledger tuning needs these on ``no_setup`` rows; without them
    every reject looks identical.
    """
    if len(trigger) < 20 or len(bias) < 20:
        return {}
    trigger_stack, trigger_emas = _ema_stack(trigger)
    bias_stack, bias_emas = _ema_stack(bias)
    high, low = structure_levels(trigger, rules.breakout_lookback)
    latest = trigger[-1]
    return {
        "rsi": rsi(trigger, rules.rsi_periods),
        "relative_volume": relative_volume(trigger, rules.volume_lookback),
        "ema9": trigger_emas[0],
        "ema21": trigger_emas[1],
        "ema50": trigger_emas[2],
        "bias_ema9": bias_emas[0],
        "bias_ema21": bias_emas[1],
        "bias_ema50": bias_emas[2],
        "trigger_ema_stack": trigger_stack,
        "bias_ema_stack": bias_stack,
        "structure_high": high,
        "structure_low": low,
        "close_above_structure_high": bool(high > 0 and latest.close > high),
        "compressed": volatility_compression(
            trigger,
            rules.compression_lookback,
            rules.compression_recent,
            rules.compression_ratio_max,
        ),
    }


def detect_price_setup(
    trigger: list[Candle],
    bias: list[Candle],
    rules: EntryRulesConfig,
    tier: TierConfig,
    strategy_name: str,
    benchmark: list[Candle] | None = None,
) -> PriceSetup | None:
    """Detect a long setup from fully deterministic OHLCV rules."""
    needed = max(51, rules.breakout_lookback + rules.retest_window + 2)
    if rules.setup_family == "bear_rally":
        needed = max(
            needed,
            rules.rsi_periods + rules.rsi_lookback_bars + 2,
            rules.failed_breakdown_lookback + rules.retest_window + 2,
            rules.rs_lookback_bars + 2,
        )
    if len(trigger) < needed or len(bias) < 50:
        return None

    if rules.setup_family == "bear_rally":
        return _detect_bear_rally_setup(
            trigger, bias, rules, tier, strategy_name, benchmark=benchmark
        )
    return _detect_bull_breakout_setup(trigger, bias, rules, tier, strategy_name)


class SignalEngine:
    """Applies one strategy's entry thresholds plus tiered confirmation.

    `market` is optional: without it the price gates cannot be evaluated and are
    skipped, which keeps unit tests focused on social logic. The runner always
    supplies one, so production entries are always price-confirmed.
    """

    def __init__(
        self,
        strategy: StrategyConfig,
        universe: UniverseConfig,
        signals: SignalsConfig | None = None,
        market: MarketData | None = None,
        market_cfg: MarketConfig | None = None,
    ):
        self.strategy = strategy
        self.universe = universe
        self.signals = signals if signals is not None else get_signals()
        self.market = market
        self.market_cfg = market_cfg if market_cfg is not None else get_market()

    # ---- Gates --------------------------------------------------------------

    def _social_ok(self, s: ScoreResult, tier: TierConfig) -> GateResult:
        st = self.strategy
        min_z = st.signal_min_zscore * tier.zscore_mult
        if s.zscore < min_z:
            return GateResult(False, f"z={s.zscore:.2f} < {min_z:.2f}")

        min_mentions = st.signal_min_mentions * tier.min_mentions_mult
        if s.mentions_window < min_mentions:
            return GateResult(False, f"mentions={s.mentions_window} < {min_mentions:.0f}")

        if s.distinct_sources < st.signal_min_distinct_sources:
            return GateResult(
                False, f"sources={s.distinct_sources} < {st.signal_min_distinct_sources}"
            )

        if s.distinct_authors < st.signal_min_distinct_authors:
            return GateResult(
                False, f"authors={s.distinct_authors} < {st.signal_min_distinct_authors}"
            )

        # Only judge polarity once enough posts carry directional language;
        # below that the ratio is too noisy to gate on.
        enough_polarity = s.directional_posts >= self.signals.min_sentiment_posts
        if enough_polarity and s.bullish_ratio < st.signal_min_bullish_ratio:
            return GateResult(
                False,
                f"bullish={s.bullish_ratio:.0%} < {st.signal_min_bullish_ratio:.0%} "
                f"({s.directional_posts} directional posts)",
            )

        return GateResult(True, "social confirmed")

    def _confirmation_cfg(self) -> ConfirmationConfig:
        """Merge strategy-local confirmation overrides onto market defaults."""
        base = self.market_cfg.confirmation
        overrides = self.strategy.confirmation.model_dump(exclude_none=True)
        if not overrides:
            return base
        return base.model_copy(update=overrides)

    def _trend_ok(self, snap: TechnicalSnapshot | None) -> GateResult:
        conf = self._confirmation_cfg()
        if not conf.enabled:
            return GateResult(True, "trend gate not evaluated", evaluated=False)
        if snap is None:
            return GateResult(not conf.fail_closed, "no market snapshot", evaluated=False)
        if not snap.ok:
            return GateResult(
                not conf.fail_closed, f"no market data ({snap.detail})", evaluated=False
            )

        if conf.require_above_sma and not snap.above_sma:
            return GateResult(
                False, f"price {snap.price:.6f} below SMA{conf.sma_periods} {snap.sma:.6f}"
            )

        if conf.min_volume_zscore > 0 and snap.volume_z < conf.min_volume_zscore:
            return GateResult(False, f"volume z={snap.volume_z:.2f} < {conf.min_volume_zscore:.2f}")

        return GateResult(True, "trend confirmed")

    def _direction_ok(self, snap: TechnicalSnapshot | None, tier: TierConfig) -> GateResult:
        conf = self._confirmation_cfg()
        if not conf.enabled or not conf.require_positive_return:
            return GateResult(True, "direction gate not evaluated", evaluated=False)
        if snap is None:
            return GateResult(not conf.fail_closed, "no market snapshot", evaluated=False)
        if not snap.ok:
            return GateResult(
                not conf.fail_closed, f"no market data ({snap.detail})", evaluated=False
            )

        floor = max(self.strategy.confirm_min_return_pct, tier.min_trailing_return_pct)
        if snap.trailing_return < floor:
            return GateResult(
                False,
                f"{self.strategy.confirm_lookback_hours}h return "
                f"{snap.trailing_return:.2%} < {floor:.2%}",
            )
        return GateResult(
            True, f"{self.strategy.confirm_lookback_hours}h return {snap.trailing_return:.2%}"
        )

    # ---- Market plumbing ----------------------------------------------------

    def _snapshot(self, product_id: str) -> TechnicalSnapshot | None:
        if self.market is None:
            return None
        granularity = self.market_cfg.candle_granularity_seconds
        periods = max(1, (self.strategy.confirm_lookback_hours * 3600) // granularity)
        return self.market.snapshot(
            product_id,
            sma_periods=self.market_cfg.confirmation.sma_periods,
            lookback_periods=periods,
        )

    def _regime(self) -> tuple[bool, bool, str]:
        """Return (risk_on, risk_off, detail).

        ``risk_on`` means full bull RISK-ON. ``risk_off`` means BTC daily close
        at/below SMA — the only unlock for ``risk_off_only`` strategies.
        """
        if self.market is None:
            return True, False, "regime not evaluated"
        assessment = getattr(self.market, "regime_assessment", None)
        if callable(assessment):
            return assessment()
        risk_on, detail = self.market.regime_ok()
        return risk_on, False, detail

    def _price_setup(self, product_id: str, tier: TierConfig) -> _PriceSetupResult:
        rules = self.strategy.entry
        granularity = rules.trigger_granularity_seconds
        fallback_ts = int(time.time()) // granularity * granularity - granularity
        if not self.market_cfg.price_action_enabled:
            return _PriceSetupResult(
                None,
                False,
                "price action disabled",
                trigger_ts=fallback_ts,
            )
        if self.market is None:
            return _PriceSetupResult(
                None,
                False,
                "market provider unavailable",
                trigger_ts=fallback_ts,
            )
        trigger = self.market.candles(product_id, rules.trigger_granularity_seconds)
        bias = self.market.candles(product_id, rules.bias_granularity_seconds)
        benchmark: list[Candle] | None = None
        if rules.setup_family == "bear_rally" and rules.allow_rs_bounce:
            bench_product = self.market_cfg.regime.benchmark_product_id
            if product_id != bench_product:
                benchmark = self.market.candles(bench_product, rules.trigger_granularity_seconds)
        latest = trigger[-1] if trigger else None
        trigger_ts = latest.ts if latest is not None else fallback_ts
        features: dict[str, str | float | bool] = {
            "trigger_candles": len(trigger),
            "bias_candles": len(bias),
        }
        if latest is not None:
            features.update(
                {
                    "trigger_open": latest.open,
                    "trigger_high": latest.high,
                    "trigger_low": latest.low,
                    "trigger_close": latest.close,
                    "trigger_volume": latest.volume,
                }
            )
        needed = max(51, rules.breakout_lookback + rules.retest_window + 2)
        if rules.setup_family == "bear_rally":
            needed = max(
                needed,
                rules.rsi_periods + rules.rsi_lookback_bars + 2,
                rules.failed_breakdown_lookback + rules.retest_window + 2,
                rules.rs_lookback_bars + 2,
            )
        if len(trigger) < needed or len(bias) < 50:
            return _PriceSetupResult(
                None,
                False,
                f"insufficient setup data (trigger={len(trigger)}, bias={len(bias)})",
                trigger_ts=trigger_ts,
                trigger_close=latest.close if latest is not None else 0.0,
                features=features,
            )
        setup = detect_price_setup(
            trigger,
            bias,
            rules,
            tier,
            self.strategy.name,
            benchmark=benchmark,
        )
        if setup is not None:
            features.update(setup.metadata)
        else:
            features.update(_setup_reject_diagnostics(trigger, bias, rules))
        return _PriceSetupResult(
            setup,
            True,
            "setup evaluated",
            trigger_ts=trigger_ts,
            trigger_close=latest.close if latest is not None else 0.0,
            features=features,
        )

    # ---- Entry point --------------------------------------------------------

    def candidates(self, scores: list[ScoreResult]) -> list[TradeCandidate]:
        risk_on, risk_off, regime_detail = self._regime()
        if not self.strategy.regime_allows_entries(risk_on, risk_off=risk_off):
            log.info("[%s] regime gate: no new entries (%s)", self.strategy.name, regime_detail)
            return []

        out: list[TradeCandidate] = []
        self._candidate_regime = (risk_on, risk_off, regime_detail)
        try:
            for s in scores:
                cand = self._evaluate(s)
                if cand is not None:
                    out.append(cand)
        finally:
            del self._candidate_regime
        if self.market_cfg.price_action_enabled:
            out.sort(key=_candidate_rank)
        return out

    def _evaluate(self, s: ScoreResult) -> TradeCandidate | None:
        cached_regime = vars(self).get("_candidate_regime")
        if cached_regime is not None:
            risk_on, risk_off, regime_detail = cached_regime
        else:
            risk_on, risk_off, regime_detail = self._regime()
        return self._evaluate_with_audit(s, risk_on, risk_off, regime_detail).candidate

    def evaluations(self, scores: list[ScoreResult]) -> list[SignalEvaluation]:
        """Evaluate every scored symbol, including all non-candidate outcomes."""
        risk_on, risk_off, regime_detail = self._regime()
        rows = [
            self._evaluate_with_audit(score, risk_on, risk_off, regime_detail) for score in scores
        ]
        return rows

    def ranked_candidates(self, evaluations: list[SignalEvaluation]) -> list[TradeCandidate]:
        candidates = [
            evaluation.candidate for evaluation in evaluations if evaluation.candidate is not None
        ]
        if self.market_cfg.price_action_enabled:
            candidates.sort(key=_candidate_rank)
        return candidates

    def _evaluate_with_audit(
        self,
        s: ScoreResult,
        risk_on: bool,
        risk_off: bool,
        regime_detail: str,
    ) -> SignalEvaluation:
        st = self.strategy
        regime_allows = st.regime_allows_entries(risk_on, risk_off=risk_off)
        granularity = st.entry.trigger_granularity_seconds
        fallback_ts = int(time.time()) // granularity * granularity - granularity
        tier_name = self.universe.tier_of(s.ticker, self.signals.default_tier)
        tier = self.signals.tier(tier_name)
        spec = self.universe.symbols.get(s.ticker)
        product_id = spec.product_id if spec is not None else ""
        score_features: dict[str, str | float | bool] = {
            "zscore": s.zscore,
            "recent_social_volume": s.recent,
            "social_baseline_mean": s.baseline_mean,
            "mentions_window": s.mentions_window,
            "distinct_sources": s.distinct_sources,
            "distinct_authors": s.distinct_authors,
            "bullish_ratio": s.bullish_ratio,
            "directional_posts": s.directional_posts,
            "social_engagement": s.engagement_total,
            "social_baseline_kind": s.baseline_kind,
            "regime_mode": st.regime_mode,
            "risk_on": risk_on,
            "risk_off": risk_off,
        }

        def result(
            *,
            price_result: _PriceSetupResult | None,
            outcome_status: str,
            outcome_reason: str,
            price_status: str = "not_evaluated",
            price_reason: str = "",
            setup_status: str = "not_evaluated",
            setup_name: str = "",
            setup_reason: str = "",
            confirmation_status: str = "not_evaluated",
            confirmation_reason: str = "",
            social_status: str = "not_evaluated",
            social_reason: str = "",
            candidate: TradeCandidate | None = None,
            extra_features: dict[str, str | float | bool] | None = None,
        ) -> SignalEvaluation:
            price_fields = vars(price_result) if price_result is not None else {}
            trigger_ts = (
                int(price_fields.get("trigger_ts", 0))
                if int(price_fields.get("trigger_ts", 0)) > 0
                else fallback_ts
            )
            features = dict(score_features)
            if price_result is not None:
                features.update(price_fields.get("features", {}))
            if extra_features:
                features.update(extra_features)
            return SignalEvaluation(
                ticker=s.ticker,
                product_id=product_id,
                strategy=st.name,
                tier=tier_name,
                trigger_granularity_seconds=granularity,
                trigger_candle_ts=trigger_ts,
                trigger_closed_at=datetime.fromtimestamp(trigger_ts + granularity, tz=UTC),
                outcome_status=outcome_status,
                outcome_reason=outcome_reason,
                regime_status="passed" if regime_allows else "blocked",
                regime_reason=regime_detail,
                price_status=price_status,
                price_reason=price_reason,
                setup_status=setup_status,
                setup_name=setup_name,
                setup_reason=setup_reason,
                confirmation_status=confirmation_status,
                confirmation_reason=confirmation_reason,
                social_status=social_status,
                social_reason=social_reason,
                feature_snapshot=features,
                candidate=candidate,
            )

        if spec is None or not self.universe.tradeable(s.ticker):
            return result(
                price_result=None,
                outcome_status="insufficient_data",
                outcome_reason="symbol is not tradeable",
                price_status="unavailable",
                price_reason="symbol is not tradeable",
            )

        if st.allowed_tickers and s.ticker.upper() not in st.allowed_tickers:
            return result(
                price_result=None,
                outcome_status="filtered",
                outcome_reason=f"ticker {s.ticker} not in strategy allowlist",
                price_status="filtered",
                price_reason=f"allowed_tickers={st.allowed_tickers}",
            )
        if st.allowed_tiers and tier_name not in st.allowed_tiers:
            return result(
                price_result=None,
                outcome_status="filtered",
                outcome_reason=f"tier {tier_name} not in strategy allowlist",
                price_status="filtered",
                price_reason=f"allowed_tiers={st.allowed_tiers}",
            )

        # Fetch the trigger identity even during a regime block so every symbol
        # gets exactly one prospective row for that closed candle.
        price_result = self._price_setup(product_id, tier)
        price_setup = price_result.setup
        if not regime_allows:
            return result(
                price_result=price_result,
                outcome_status="regime_blocked",
                outcome_reason=regime_detail,
                price_status="available" if price_result.trigger_close > 0 else "unavailable",
                price_reason=price_result.detail,
                setup_status="not_evaluated",
                setup_reason="regime blocked before setup decision",
            )

        if self.market_cfg.price_action_enabled and price_setup is None:
            if price_result.evaluated:
                log.debug("[%s] %s rejected: no qualifying price setup", st.name, s.ticker)
                return result(
                    price_result=price_result,
                    outcome_status="no_setup",
                    outcome_reason="no qualifying price setup",
                    price_status="available",
                    price_reason=price_result.detail,
                    setup_status="rejected",
                    setup_reason="no qualifying price setup",
                )
            if self.market_cfg.price_action_fail_closed:
                log.warning(
                    "[%s] %s rejected: price setup unavailable (%s)",
                    st.name,
                    s.ticker,
                    price_result.detail,
                )
                return result(
                    price_result=price_result,
                    outcome_status="insufficient_data",
                    outcome_reason=price_result.detail,
                    price_status="unavailable",
                    price_reason=price_result.detail,
                    setup_status="not_evaluated",
                    setup_reason=price_result.detail,
                )

        snap = None
        trend = GateResult(True, "trend not required", evaluated=False)
        direction = GateResult(True, "price action confirmed")
        confirmation_features: dict[str, str | float | bool] = {}
        if self.market_cfg.price_action_enabled:
            snap = self._snapshot(product_id)
            trend = self._trend_ok(snap)
            if snap is not None:
                snapshot_fields = vars(snap)
                confirmation_features.update(
                    {
                        "snapshot_ok": snap.ok,
                        "snapshot_price": snap.price,
                        "snapshot_sma": snap.sma,
                        "snapshot_volume_z": snap.volume_z,
                        "snapshot_trailing_return": snap.trailing_return,
                        "snapshot_atr_pct": float(snapshot_fields.get("atr_pct", 0.0)),
                    }
                )
            if not trend.passed:
                log.debug("[%s] %s rejected: %s", st.name, s.ticker, trend.why)
                return result(
                    price_result=price_result,
                    outcome_status="confirmation_reject",
                    outcome_reason=trend.why,
                    price_status="available",
                    price_reason=price_result.detail,
                    setup_status="passed",
                    setup_name=price_setup.name if price_setup else "",
                    setup_reason="qualifying setup",
                    confirmation_status="rejected",
                    confirmation_reason=trend.why,
                    extra_features=confirmation_features,
                )
            direction = self._direction_ok(snap, tier)
            if not direction.passed:
                log.debug("[%s] %s rejected: %s", st.name, s.ticker, direction.why)
                return result(
                    price_result=price_result,
                    outcome_status="confirmation_reject",
                    outcome_reason=direction.why,
                    price_status="available",
                    price_reason=price_result.detail,
                    setup_status="passed",
                    setup_name=price_setup.name if price_setup else "",
                    setup_reason="qualifying setup",
                    confirmation_status="rejected",
                    confirmation_reason=f"{trend.why}; {direction.why}",
                    extra_features=confirmation_features,
                )

        social = GateResult(True, "social ignored", evaluated=False)
        size_multiplier = price_setup.conviction if price_setup else 1.0
        social_decision = "ignored"
        shadow = self.signals.social_decision_mode == "shadow"
        social_rejected = False
        if tier.social_policy in ("required", "catalyst"):
            social = self._social_ok(s, tier)
            social_decision = "would_pass" if social.passed else "would_reject"
            social_rejected = not social.passed and not shadow
        elif tier.social_policy == "optional":
            if (
                s.directional_posts >= self.signals.min_sentiment_posts
                and s.bullish_ratio < tier.social_veto_bullish_ratio
            ):
                social = GateResult(False, "bearish social veto")
                social_decision = "would_reject"
                social_rejected = not shadow
            else:
                social = self._social_ok(s, tier)
                social_decision = "would_boost" if social.passed else "would_pass"
            if social.passed and not shadow:
                size_multiplier = min(size_multiplier * tier.optional_social_boost, 1.0)

        # Explicit price-action-disabled mode exists only for deterministic
        # offline simulation and focused social unit tests.
        if not self.market_cfg.price_action_enabled:
            needs_social = tier.signal_mode in ("social", "hybrid")
            needs_trend = tier.signal_mode in ("trend", "hybrid")
            if needs_social and tier.social_policy == "ignored":
                social = self._social_ok(s, tier)
                social_decision = "would_pass" if social.passed else "would_reject"
                social_rejected = not social.passed and not shadow
            snap = self._snapshot(product_id)
            if needs_trend:
                trend = self._trend_ok(snap)
                if not trend.passed or (tier.signal_mode == "trend" and not trend.evaluated):
                    return result(
                        price_result=price_result,
                        outcome_status="confirmation_reject",
                        outcome_reason=trend.why,
                        price_status="disabled",
                        price_reason=price_result.detail,
                        setup_status="disabled",
                        setup_name="offline_social",
                        setup_reason="price action disabled",
                        confirmation_status="rejected",
                        confirmation_reason=trend.why,
                    )
            direction = self._direction_ok(snap, tier)
            if not direction.passed:
                return result(
                    price_result=price_result,
                    outcome_status="confirmation_reject",
                    outcome_reason=direction.why,
                    price_status="disabled",
                    price_reason=price_result.detail,
                    setup_status="disabled",
                    setup_name="offline_social",
                    setup_reason="price action disabled",
                    confirmation_status="rejected",
                    confirmation_reason=direction.why,
                )

        if social_rejected:
            log.debug("[%s] %s rejected: %s", st.name, s.ticker, social.why)
            return result(
                price_result=price_result,
                outcome_status="confirmation_reject",
                outcome_reason=social.why,
                price_status=("available" if self.market_cfg.price_action_enabled else "disabled"),
                price_reason=price_result.detail,
                setup_status=("passed" if self.market_cfg.price_action_enabled else "disabled"),
                setup_name=price_setup.name if price_setup else "offline_social",
                setup_reason=(
                    "qualifying setup"
                    if self.market_cfg.price_action_enabled
                    else "price action disabled"
                ),
                confirmation_status="rejected",
                confirmation_reason=social.why,
                social_status="rejected",
                social_reason=social.why,
                extra_features=confirmation_features,
            )

        reason = (
            f"[{tier_name}/{tier.social_policy}] "
            f"{price_setup.name if price_setup else 'offline'}; {social.why}"
        )
        if shadow and tier.social_policy != "ignored":
            log.info(
                "SHADOW social[%s] %s %s: %s (size unchanged)",
                st.name,
                s.ticker,
                social_decision,
                social.why,
            )
        log.info(
            "SIGNAL[%s] %s tier=%s mode=%s z=%.2f mentions=%d authors=%d bullish=%.0f%%",
            st.name,
            s.ticker,
            tier_name,
            tier.signal_mode,
            s.zscore,
            s.mentions_window,
            s.distinct_authors,
            s.bullish_ratio * 100,
        )
        setup_metadata = dict(price_setup.metadata) if price_setup else {}
        if price_setup:
            setup_metadata["price_conviction"] = price_setup.conviction
        candidate = TradeCandidate(
            ticker=s.ticker,
            product_id=product_id,
            zscore=s.zscore,
            mentions=s.mentions_window,
            sources=s.distinct_sources,
            reason=reason,
            strategy=st.name,
            authors=s.distinct_authors,
            bullish_ratio=s.bullish_ratio,
            tier=tier_name,
            atr_pct=(
                price_setup.atr_pct if price_setup else (snap.atr_pct if snap and snap.ok else 0.0)
            ),
            trailing_return=snap.trailing_return if snap and snap.ok else 0.0,
            setup=price_setup.name if price_setup else "offline_social",
            entry_price=price_setup.entry_price if price_setup else 0.0,
            structure_stop=price_setup.structure_stop if price_setup else 0.0,
            stop_pct=price_setup.stop_pct if price_setup else 0.0,
            conviction=size_multiplier,
            size_multiplier=size_multiplier,
            setup_metadata=setup_metadata,
            count_volume=s.mentions_window,
            engagement=s.engagement_total,
            decision_mode=self.signals.social_decision_mode,
            social_decision=social_decision,
            social_reason=social.why,
        )
        return result(
            price_result=price_result,
            outcome_status="candidate",
            outcome_reason=reason,
            price_status=("available" if self.market_cfg.price_action_enabled else "disabled"),
            price_reason=price_result.detail,
            setup_status=("passed" if self.market_cfg.price_action_enabled else "disabled"),
            setup_name=candidate.setup,
            setup_reason=(
                "qualifying setup"
                if self.market_cfg.price_action_enabled
                else "price action disabled"
            ),
            confirmation_status="passed",
            confirmation_reason=f"{trend.why}; {direction.why}",
            social_status=(
                "passed" if social.passed else ("shadow_reject" if shadow else "rejected")
            ),
            social_reason=social.why,
            candidate=candidate,
            extra_features=confirmation_features,
        )

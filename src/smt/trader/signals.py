"""Price-action-first signal engine with tier-specific social playbooks.

Production candidates always begin with a deterministic multi-timeframe setup:
EMA 9/21/50 trend alignment, RSI, structure, relative volume, and one of a
breakout-close, breakout-retest, or permitted intraday VWAP-reclaim pattern.
Social is then ignored, optional, required, or catalyst-required by liquidity
tier; it can never create a candidate without the price setup.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..config import (
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


def _ema_stack(candles: list[Candle]) -> tuple[bool, tuple[float, float, float]]:
    values = (ema(candles, 9), ema(candles, 21), ema(candles, 50))
    return all(values) and values[0] > values[1] > values[2], values


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


def detect_price_setup(
    trigger: list[Candle],
    bias: list[Candle],
    rules: EntryRulesConfig,
    tier: TierConfig,
    strategy_name: str,
) -> PriceSetup | None:
    """Detect a long setup from fully deterministic OHLCV rules."""
    needed = max(51, rules.breakout_lookback + rules.retest_window + 2)
    if len(trigger) < needed or len(bias) < 50:
        return None

    trigger_stack, trigger_emas = _ema_stack(trigger)
    bias_stack, bias_emas = _ema_stack(bias)
    if rules.require_trigger_ema_stack and not trigger_stack:
        return None
    if rules.require_bias_ema_stack and not bias_stack:
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
    elif (
        strategy_name == "intraday"
        and rules.allow_vwap_pullback
        and tier.allow_vwap_pullback
    ):
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

    atr_abs = atr(trigger, 14)
    structure_stop = stop_reference - atr_abs * rules.stop_atr_buffer
    stop_pct = (latest.close - structure_stop) / latest.close
    if stop_pct < rules.min_stop_pct:
        stop_pct = rules.min_stop_pct
        structure_stop = latest.close * (1.0 - stop_pct)
    if stop_pct > rules.max_stop_pct or structure_stop <= 0:
        return None

    conviction = {
        "breakout_close": 0.85,
        "breakout_retest": 1.0,
        "vwap_pullback": 0.90,
    }[setup_name]
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
    }
    return PriceSetup(
        name=setup_name,
        entry_price=latest.close,
        structure_stop=structure_stop,
        stop_pct=stop_pct,
        atr_pct=(atr_abs / latest.close) if latest.close > 0 else 0.0,
        conviction=conviction,
        metadata=metadata,
    )


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

    def _trend_ok(self, snap: TechnicalSnapshot | None) -> GateResult:
        conf = self.market_cfg.confirmation
        if snap is None or not conf.enabled:
            return GateResult(True, "trend gate not evaluated", evaluated=False)
        if not snap.ok:
            return GateResult(
                not conf.fail_closed, f"no market data ({snap.detail})", evaluated=False
            )

        if conf.require_above_sma and not snap.above_sma:
            return GateResult(
                False, f"price {snap.price:.6f} below SMA{conf.sma_periods} {snap.sma:.6f}"
            )

        if snap.volume_z < conf.min_volume_zscore:
            return GateResult(
                False, f"volume z={snap.volume_z:.2f} < {conf.min_volume_zscore:.2f}"
            )

        return GateResult(True, "trend confirmed")

    def _direction_ok(self, snap: TechnicalSnapshot | None, tier: TierConfig) -> GateResult:
        conf = self.market_cfg.confirmation
        if snap is None or not conf.enabled or not conf.require_positive_return:
            return GateResult(True, "direction gate not evaluated", evaluated=False)
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

    def _regime(self) -> tuple[bool, str]:
        if self.market is None:
            return True, "regime not evaluated"
        state_reader = getattr(self.market, "regime_state", None)
        if callable(state_reader):
            state, detail = state_reader()
            allowed = (
                self.strategy.regime_mode == "any"
                or state == "any"
                or (
                    self.strategy.regime_mode == "risk_on_only"
                    and state == "risk_on"
                )
                or (
                    self.strategy.regime_mode == "risk_off_only"
                    and state == "risk_off"
                )
            )
            return allowed, f"{state}: {detail}"
        risk_on, detail = self.market.regime_ok()
        if self.strategy.regime_mode == "risk_off_only":
            return not risk_on, detail
        return risk_on, detail

    def _price_setup(self, product_id: str, tier: TierConfig) -> PriceSetup | None:
        if not self.market_cfg.price_action_enabled:
            return None
        if self.market is None:
            return None
        rules = self.strategy.entry
        trigger = self.market.candles(product_id, rules.trigger_granularity_seconds)
        bias = self.market.candles(product_id, rules.bias_granularity_seconds)
        return detect_price_setup(trigger, bias, rules, tier, self.strategy.name)

    # ---- Entry point --------------------------------------------------------

    def candidates(self, scores: list[ScoreResult]) -> list[TradeCandidate]:
        regime_ok, regime_detail = self._regime()
        if not regime_ok:
            log.info("[%s] regime gate: no new entries (%s)", self.strategy.name, regime_detail)
            return []

        out: list[TradeCandidate] = []
        for s in scores:
            cand = self._evaluate(s)
            if cand is not None:
                out.append(cand)
        return out

    def _evaluate(self, s: ScoreResult) -> TradeCandidate | None:
        if not self.universe.tradeable(s.ticker):
            return None

        st = self.strategy
        if st.allowed_tickers and s.ticker not in st.allowed_tickers:
            return None
        tier_name = self.universe.tier_of(s.ticker, self.signals.default_tier)
        tier = self.signals.tier(tier_name)
        spec = self.universe.symbols[s.ticker]

        # Production is price-action-first: every tier, including social-led
        # micro caps, must have a deterministic trigger before social is read.
        price_setup = self._price_setup(spec.product_id, tier)
        if self.market_cfg.price_action_enabled and price_setup is None:
            log.debug("[%s] %s rejected: no qualifying price setup", st.name, s.ticker)
            return None

        social = GateResult(True, "social ignored", evaluated=False)
        size_multiplier = price_setup.conviction if price_setup else 1.0
        social_decision = "ignored"
        shadow = self.signals.social_decision_mode == "shadow"
        if tier.social_policy in ("required", "catalyst"):
            social = self._social_ok(s, tier)
            social_decision = "would_pass" if social.passed else "would_reject"
            if not social.passed and not shadow:
                log.debug("[%s] %s rejected: %s", st.name, s.ticker, social.why)
                return None
        elif tier.social_policy == "optional":
            if (
                s.directional_posts >= self.signals.min_sentiment_posts
                and s.bullish_ratio < tier.social_veto_bullish_ratio
            ):
                social = GateResult(False, "bearish social veto")
                social_decision = "would_reject"
                if not shadow:
                    log.info("[%s] %s vetoed by bearish social confirmation", st.name, s.ticker)
                    return None
            else:
                social = self._social_ok(s, tier)
                social_decision = "would_boost" if social.passed else "would_pass"
            if social.passed and not shadow:
                size_multiplier = min(size_multiplier * tier.optional_social_boost, 1.0)

        # Explicit price-action-disabled mode exists only for deterministic
        # offline simulation and focused social unit tests.
        snap = None
        direction = GateResult(True, "price action confirmed")
        if not self.market_cfg.price_action_enabled:
            needs_social = tier.signal_mode in ("social", "hybrid")
            needs_trend = tier.signal_mode in ("trend", "hybrid")
            if needs_social and tier.social_policy == "ignored":
                social = self._social_ok(s, tier)
                social_decision = "would_pass" if social.passed else "would_reject"
                if not social.passed and not shadow:
                    return None
            snap = self._snapshot(spec.product_id)
            if needs_trend:
                trend = self._trend_ok(snap)
                if not trend.passed or (tier.signal_mode == "trend" and not trend.evaluated):
                    return None
            direction = self._direction_ok(snap, tier)
            if not direction.passed:
                return None

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
        return TradeCandidate(
            ticker=s.ticker,
            product_id=spec.product_id,
            zscore=s.zscore,
            mentions=s.mentions_window,
            sources=s.distinct_sources,
            reason=reason,
            strategy=st.name,
            authors=s.distinct_authors,
            bullish_ratio=s.bullish_ratio,
            tier=tier_name,
            atr_pct=(
                price_setup.atr_pct
                if price_setup
                else (snap.atr_pct if snap and snap.ok else 0.0)
            ),
            trailing_return=snap.trailing_return if snap and snap.ok else 0.0,
            setup=price_setup.name if price_setup else "offline_social",
            entry_price=price_setup.entry_price if price_setup else 0.0,
            structure_stop=price_setup.structure_stop if price_setup else 0.0,
            stop_pct=price_setup.stop_pct if price_setup else 0.0,
            conviction=size_multiplier,
            size_multiplier=size_multiplier,
            setup_metadata=price_setup.metadata if price_setup else {},
            count_volume=s.mentions_window,
            engagement=s.engagement_total,
            decision_mode=self.signals.social_decision_mode,
            social_decision=social_decision,
            social_reason=social.why,
        )

"""Focused prospective-ledger and no-lookahead outcome coverage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _helpers import make_store, make_strategy, make_universe

from smt.config import MarketConfig, SignalsConfig, TierConfig
from smt.market import Candle, TechnicalSnapshot
from smt.models import utcnow
from smt.policy import trading_policy_identity
from smt.scorer import ScoreResult
from smt.store import (
    OPPORTUNITY_LEDGER_VERSION,
    opportunity_key,
    stable_config_fingerprint,
)
from smt.trader.signals import SignalEngine


def test_ledger_default_uses_canonical_trading_policy_fingerprint():
    assert stable_config_fingerprint() == trading_policy_identity().fingerprint
    assert stable_config_fingerprint() != "unversioned-v1"


def _score() -> ScoreResult:
    return ScoreResult(
        ticker="SOL",
        zscore=6.0,
        recent=30.0,
        baseline_mean=2.0,
        mentions_window=40,
        distinct_sources=2,
        distinct_authors=12,
        bullish_ratio=0.9,
        directional_posts=20,
        baseline_kind="count_trailing",
        reason="bounded numeric test",
        engagement_total=15,
    )


def _candles(count: int = 70, *, breakout: bool = False) -> list[Candle]:
    rows = [
        Candle(
            ts=index * 900,
            open=100.0 + index * 0.2,
            high=100.15 + index * 0.2,
            low=99.9 + index * 0.2,
            close=100.1 + index * 0.2,
            volume=100.0,
        )
        for index in range(count)
    ]
    if breakout:
        level = max(candle.high for candle in rows[-21:-1])
        rows[-1] = Candle(
            ts=rows[-1].ts,
            open=level - 0.1,
            high=level + 1.2,
            low=level - 0.2,
            close=level + 1.0,
            volume=250.0,
        )
    return rows


def _flat_candles(count: int = 70) -> list[Candle]:
    return [Candle(index * 900, 99.8, 100.2, 100.0, 100.0, 100.0) for index in range(count)]


def _bias_candles(count: int = 70) -> list[Candle]:
    return [
        Candle(
            ts=index * 3_600,
            open=100.0 + index * 0.2,
            high=100.2 + index * 0.2,
            low=99.9 + index * 0.2,
            close=100.1 + index * 0.2,
            volume=100.0,
        )
        for index in range(count)
    ]


class _AuditMarket:
    def __init__(
        self,
        *,
        trigger: list[Candle],
        regime_ok: bool = True,
        confirmation_ok: bool = True,
    ):
        self.trigger = trigger
        self.regime = regime_ok
        self.confirmation_ok = confirmation_ok

    def candles(self, _product: str, granularity: int | None = None) -> list[Candle]:
        if granularity == 900:
            return self.trigger
        return _bias_candles()

    def regime_ok(self) -> tuple[bool, str]:
        return self.regime, "test regime"

    def snapshot(self, product: str, sma_periods: int, lookback_periods: int) -> TechnicalSnapshot:
        del sma_periods, lookback_periods
        return TechnicalSnapshot(
            product_id=product,
            ok=True,
            price=110.0 if self.confirmation_ok else 90.0,
            sma=100.0,
            trailing_return=0.05,
            volume_z=2.0,
            atr_pct=0.02,
        )


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        (_AuditMarket(trigger=[]), "insufficient_data"),
        (_AuditMarket(trigger=_candles(), regime_ok=False), "regime_blocked"),
        (_AuditMarket(trigger=_flat_candles()), "no_setup"),
        (
            _AuditMarket(trigger=_candles(breakout=True), confirmation_ok=False),
            "confirmation_reject",
        ),
        (_AuditMarket(trigger=_candles(breakout=True)), "candidate"),
    ],
)
def test_signal_evaluations_cover_candidate_and_non_candidate_outcomes(market, expected):
    cfg = MarketConfig()
    cfg.confirmation.min_volume_zscore = 1.0
    engine = SignalEngine(
        make_strategy(),
        make_universe(),
        signals=SignalsConfig(
            tiers={
                "mid": TierConfig(
                    social_policy="ignored",
                    retest_policy="preferred",
                    min_relative_volume=2.0,
                )
            }
        ),
        market=market,
        market_cfg=cfg,
    )

    evaluation = engine.evaluations([_score()])[0]

    assert evaluation.outcome_status == expected
    assert evaluation.trigger_candle_ts >= 0
    assert (evaluation.candidate is not None) is (expected == "candidate")


def _opportunity_values(*, key: str, evaluated_at: datetime | None = None) -> dict:
    evaluated = evaluated_at or utcnow()
    return {
        "opportunity_key": key,
        "ledger_version": OPPORTUNITY_LEDGER_VERSION,
        "config_fingerprint": stable_config_fingerprint(),
        "run_id": "run-1",
        "strategy": "intraday",
        "ticker": "SOL",
        "product_id": "SOL-USD",
        "tier": "mid",
        "trigger_granularity_seconds": 900,
        "trigger_candle_ts": 1_800_000_000,
        "trigger_closed_at": datetime.fromtimestamp(1_800_000_900, tz=UTC),
        "outcome_status": "candidate",
        "outcome_reason": "candidate",
        "regime_status": "passed",
        "price_status": "available",
        "setup_status": "passed",
        "confirmation_status": "passed",
        "social_status": "shadow_reject",
        "llm_status": "not_evaluated",
        "risk_status": "not_evaluated",
        "execution_status": "not_evaluated",
        "feature_snapshot": {"trigger_close": 100.0, "distinct_authors": 12},
        "proposed_entry_price": 100.0,
        "proposed_stop_price": 95.0,
        "evaluated_at": evaluated,
    }


def test_opportunity_upsert_deduplicates_without_erasing_enrichment(tmp_path):
    store = make_store(tmp_path)
    fingerprint = stable_config_fingerprint()
    key = opportunity_key(
        config_fingerprint=fingerprint,
        run_id="run-1",
        strategy="intraday",
        ticker="SOL",
        trigger_candle_ts=1_800_000_000,
    )
    values = _opportunity_values(key=key)
    store.upsert_opportunity(**values)
    store.enrich_opportunity(
        key,
        llm_status="complete",
        risk_status="approved",
        proposed_notional_usd=250.0,
        execution_status="opened",
        trade_id=7,
    )
    store.upsert_opportunity(**{**values, "outcome_reason": "candidate refreshed"})

    rows = store.opportunities()
    assert len(rows) == 1
    assert rows[0].outcome_reason == "candidate refreshed"
    assert rows[0].llm_status == "complete"
    assert rows[0].risk_status == "approved"
    assert rows[0].execution_status == "opened"
    assert rows[0].trade_id == 7


def test_shadow_and_trade_link_through_opportunity_key(tmp_path):
    store = make_store(tmp_path)
    key = "opportunity-link"
    store.upsert_opportunity(**_opportunity_values(key=key))
    store.upsert_shadow_decision(
        decision_key="shadow-link",
        opportunity_key=key,
        ticker="SOL",
        strategy="intraday",
        risk_status="approved",
    )

    assert store.link_shadow_trade("shadow-link", 42)
    assert store.shadow_decision("shadow-link").opportunity_key == key
    assert store.shadow_decision("shadow-link").trade_id == 42
    assert store.opportunity(key).trade_id == 42


def test_maturation_uses_only_post_evaluation_pre_horizon_candles(tmp_path):
    store = make_store(tmp_path)
    evaluated = datetime(2026, 8, 9, 0, 5, tzinfo=UTC)
    key = "prospective-only"
    values = _opportunity_values(key=key, evaluated_at=evaluated)
    values["trigger_candle_ts"] = int((evaluated - timedelta(minutes=20)).timestamp())
    store.upsert_opportunity(**values)
    base = int(evaluated.replace(minute=0).timestamp())
    candles = [
        Candle(base, 1.0, 1_000.0, 100.0, 100.0, 1.0),
        Candle(base + 900, 95.0, 104.0, 100.0, 101.0, 1.0),
        Candle(base + 1_800, 98.0, 106.0, 101.0, 102.0, 1.0),
        Candle(base + 2_700, 99.0, 105.0, 102.0, 103.0, 1.0),
        Candle(base + 3_600, 1.0, 1_000.0, 103.0, 999.0, 1.0),
    ]

    assert not store.mature_opportunity(
        key,
        candles,
        as_of=evaluated + timedelta(minutes=59),
    )
    assert store.opportunity(key).return_1h is None

    assert store.mature_opportunity(
        key,
        candles,
        as_of=evaluated + timedelta(hours=1),
    )
    row = store.opportunity(key)
    assert row.return_1h == pytest.approx(0.03)
    assert row.mae_1h == pytest.approx(-0.05)
    assert row.mfe_1h == pytest.approx(0.06)
    assert row.return_4h is None

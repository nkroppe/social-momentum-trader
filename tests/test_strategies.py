"""Tests for dual-strategy support: allocation, independence, tagging, simulate."""

from __future__ import annotations

import uuid

import pytest
from _helpers import make_store, make_strategy, make_universe

from smt.config import Settings, StrategiesConfig, get_strategies
from smt.demo import seed_momentum
from smt.models import ExitReason, Trade, TradeStatus, utcnow
from smt.scorer import MomentumScorer
from smt.trader.manager import TradeManager
from smt.trader.paper import PaperBroker
from smt.trader.risk import RiskGate
from smt.trader.signals import SignalEngine, TradeCandidate


def _settings() -> Settings:
    return Settings(paper_start_equity=5000)


def test_per_strategy_allocation_sizing(tmp_path):
    """Each strategy sizes off its OWN allocation half, not total equity."""
    store = make_store(tmp_path)
    gate = RiskGate(store)

    intraday = make_strategy("intraday", allocation=0.5, max_position_pct=0.10)
    swing = make_strategy("swing", allocation=0.3, max_position_pct=0.10)

    manager = TradeManager(_settings(), make_universe(), store, PaperBroker(seed=1))

    # No trades yet -> allocation equity == start slice.
    assert manager.allocation_equity(intraday) == pytest.approx(2500.0)
    assert manager.allocation_equity(swing) == pytest.approx(1500.0)

    cand = TradeCandidate("SOL", "SOL-USD", 5.0, 20, 3, "x")
    d_intra = gate.evaluate(cand, intraday, manager.allocation_equity(intraday), 2500.0)
    d_swing = gate.evaluate(cand, swing, manager.allocation_equity(swing), 1500.0)

    # 10% of each strategy's own half.
    assert d_intra.notional_usd == pytest.approx(250.0)
    assert d_swing.notional_usd == pytest.approx(150.0)


def test_independent_limits(tmp_path):
    """One strategy at its position limit must not block the other."""
    store = make_store(tmp_path)
    gate = RiskGate(store)
    blocked = make_strategy("intraday", max_open_positions=0)
    allowed = make_strategy("swing", max_open_positions=3)

    cand = TradeCandidate("SOL", "SOL-USD", 5.0, 20, 3, "x")
    assert not gate.evaluate(cand, blocked, 2500, 2500).approved
    assert gate.evaluate(cand, allowed, 2500, 2500).approved


def test_independent_loss_halt(tmp_path):
    """A loss-halt on one strategy must not halt the other."""
    store = make_store(tmp_path)
    gate = RiskGate(store)

    # A big realized loss tagged to intraday only.
    store.add_trade(
        Trade(
            ticker="SOL",
            strategy="intraday",
            product_id="SOL-USD",
            status=TradeStatus.CLOSED,
            qty=1.0,
            entry_price=100.0,
            entry_notional=100.0,
            take_profit=106.0,
            stop_loss=97.0,
            time_stop_at=utcnow(),
            exit_price=80.0,
            exit_reason=ExitReason.STOP_LOSS,
            realized_pnl=-500.0,  # -20% of a 2500 allocation -> trips daily halt
            closed_at=utcnow(),
        )
    )

    intraday = make_strategy("intraday", daily_loss_halt_pct=-0.05)
    swing = make_strategy("swing", daily_loss_halt_pct=-0.05)

    intraday_halted, _ = gate.portfolio_halted(intraday, 2500.0)
    swing_halted, _ = gate.portfolio_halted(swing, 2500.0)
    assert intraday_halted is True
    assert swing_halted is False


def test_strategy_tag_persisted(tmp_path):
    """The opening strategy is stored on the trade and is queryable per strategy."""
    store = make_store(tmp_path)
    swing = make_strategy("swing")
    manager = TradeManager(_settings(), make_universe(), store, PaperBroker(seed=2))

    cand = TradeCandidate("BTC", "BTC-USD", 5.0, 20, 3, "x", "swing")
    trade = manager.open_position(cand, 200, swing)
    assert trade.strategy == "swing"

    assert len(store.open_trades("swing")) == 1
    assert len(store.open_trades("intraday")) == 0
    assert store.open_trade_for("BTC", "swing") is not None
    assert store.open_trade_for("BTC", "intraday") is None


def test_both_strategies_simulate_end_to_end(tmp_path):
    """Seed one ticker, open + close a position under BOTH real strategies."""
    store = make_store(tmp_path)
    universe = make_universe()
    settings = _settings()
    broker = PaperBroker(seed=3)
    gate = RiskGate(store)
    manager = TradeManager(settings, universe, store, broker)

    strategies = get_strategies().enabled()
    names = {s.name for s in strategies}
    assert {"intraday", "swing"} <= names  # ships with both

    seed_momentum(store, "SOL", strategies)

    scorers = {
        s.name: MomentumScorer(store, universe, s.scorer_bucket_minutes, s.scorer_lookback_buckets)
        for s in strategies
    }
    engines = {s.name: SignalEngine(s, universe) for s in strategies}

    for st in strategies:
        equity_alloc = manager.allocation_equity(st)
        for cand in engines[st.name].candidates(scorers[st.name].score_all()):
            decision = gate.evaluate(cand, st, equity_alloc, manager.allocation_start_equity(st))
            if decision.approved:
                manager.open_position(cand, decision.notional_usd, st)
                equity_alloc = manager.allocation_equity(st)

    # Each strategy independently opened its own SOL position.
    assert store.open_trade_for("SOL", "intraday") is not None
    assert store.open_trade_for("SOL", "swing") is not None

    # Force price above the highest TP; both should take-profit.
    highest_tp = max(t.take_profit for t in store.open_trades())
    broker.set_price("SOL-USD", highest_tp * 1.05)
    manager.manage_open_trades()

    for name in ("intraday", "swing"):
        closed = store.closed_trades_for("SOL", name)
        assert closed, f"{name} should have a closed trade"
        assert closed[-1].exit_reason == ExitReason.TAKE_PROFIT

    # Stats are reported per strategy.
    for name in ("intraday", "swing"):
        stats = store.strategy_stats(name)
        assert stats["closed_trades"] == 1


def test_allocation_sum_validation():
    """Enabled allocations summing above 1.0 are rejected."""
    a = make_strategy("intraday", allocation=0.6)
    b = make_strategy("swing", allocation=0.6)
    with pytest.raises(ValueError):
        StrategiesConfig(strategies={"intraday": a, "swing": b})


def test_time_stop_cap_validation():
    """time_stop_hours beyond the 72h cap is rejected."""
    with pytest.raises(ValueError):
        make_strategy("swing", time_stop_hours=100)
    # And a within-range custom value is accepted.
    assert make_strategy("swing", time_stop_hours=72).time_stop_hours == 72


def test_unique_external_id_dedup(tmp_path):
    """Sanity: strategy work didn't break event de-dup."""
    store = make_store(tmp_path)
    ext = uuid.uuid4().hex
    from smt.models import SocialEvent

    ev = SocialEvent(
        source="reddit", external_id=ext, ticker="SOL", created_at=utcnow(), weight=1.0
    )
    assert store.add_events([ev]) == 1
    ev2 = SocialEvent(
        source="reddit", external_id=ext, ticker="SOL", created_at=utcnow(), weight=1.0
    )
    assert store.add_events([ev2]) == 0

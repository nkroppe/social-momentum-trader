"""I3 weekly-report metric fixes: fee/gross, MFE floor, overlap, sessions, slippage."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from _helpers import make_store, make_strategy, make_universe

from smt.config import Settings
from smt.llm.config import LLMConfig
from smt.llm.reflection import (
    _ITEM_CHAR_LIMIT,
    _PROMPT_ITEM_CHAR_LIMIT,
    _SUMMARY_CHAR_LIMIT,
    WeeklyReflector,
    _clip_text,
)
from smt.models import ExitReason, Trade, TradeStatus
from smt.ops.reports import (
    MFE_CAPTURE_MIN_MFE_R,
    MFE_R_EPSILON,
    SESSION_BOUNDARIES_UTC,
    _cost_row,
    _format_fee_to_gross,
    aggregate_cost_stats,
    aggregate_entry_slippage,
    aggregate_mfe_capture,
    backfill_entry_slippage_vs_proposed,
    build_weekly_report,
    format_cross_sleeve_overlap_group,
    format_trade_row,
    same_ticker_cross_sleeve_overlaps,
    trade_mfe_capture,
    trade_session,
)
from smt.store import OPPORTUNITY_LEDGER_VERSION, opportunity_key
from smt.trader.broker import Fill
from smt.trader.manager import TradeManager, entry_slippage_snapshot
from smt.trader.signals import TradeCandidate


def _cfg_llm(tmp_path, **overrides) -> LLMConfig:
    data = {
        "budget_state_file": str(tmp_path / "budget.json"),
        "sandbox_dir": str(tmp_path / "sandbox"),
        "judge": {"state_file": str(tmp_path / "judge.json")},
        "reflection": {"state_file": str(tmp_path / "reflections.jsonl")},
    }
    data.update(overrides)
    return LLMConfig(**data)


def _trade(**kwargs) -> Trade:
    closed_at = kwargs.pop("closed_at", datetime(2026, 10, 4, 12, tzinfo=UTC))
    default_opened = (
        closed_at - timedelta(hours=3) if closed_at else datetime(2026, 10, 4, 9, tzinfo=UTC)
    )
    opened_at = kwargs.pop("opened_at", default_opened)
    status = kwargs.pop("status", TradeStatus.CLOSED if closed_at else TradeStatus.OPEN)
    values = dict(
        ticker="SOL",
        strategy="intraday",
        product_id="SOL-USD",
        qty=1.0,
        original_qty=1.0,
        entry_price=100.0,
        entry_notional=250.0,
        take_profit=110.0,
        stop_loss=95.0,
        highest_price=100.0,
        initial_risk_per_unit=10.0,
        time_stop_at=opened_at + timedelta(hours=12),
        realized_pnl=0.0,
        fees_paid=1.0,
        status=status,
        opened_at=opened_at,
        closed_at=closed_at,
    )
    values.update(kwargs)
    return Trade(**values)


def _candidate(**overrides) -> TradeCandidate:
    values = dict(
        ticker="BTC",
        product_id="BTC-USD",
        zscore=5.0,
        mentions=20,
        sources=3,
        reason="test",
        strategy="intraday",
        setup="breakout_close",
        entry_price=100.0,
        structure_stop=90.0,
        stop_pct=0.10,
    )
    values.update(overrides)
    return TradeCandidate(**values)


# ---- Fee / gross display ----------------------------------------------------


def test_cost_row_fee_to_gross_na_when_gross_not_positive_and_prints_ntl():
    closed_at = datetime(2026, 10, 4, 16, tzinfo=UTC)
    losing = _trade(realized_pnl=-5.0, fees_paid=2.0, entry_notional=200.0, closed_at=closed_at)
    flat = _trade(
        ticker="ETH",
        realized_pnl=-1.0,
        fees_paid=1.0,
        entry_notional=100.0,
        closed_at=closed_at,
    )
    winning = _trade(
        ticker="BTC",
        strategy="swing",
        realized_pnl=8.0,
        fees_paid=2.0,
        entry_notional=400.0,
        closed_at=closed_at,
        opened_at=closed_at - timedelta(hours=1),
    )
    lose_stats = aggregate_cost_stats([losing])
    flat_stats = aggregate_cost_stats([flat])
    win_stats = aggregate_cost_stats([winning])
    assert lose_stats.gross_pnl < 0
    assert lose_stats.fee_to_gross != 0
    assert flat_stats.gross_pnl == pytest.approx(0.0)
    assert _format_fee_to_gross(lose_stats) == "n/a"
    assert _format_fee_to_gross(flat_stats) == "n/a"
    assert "%" in _format_fee_to_gross(win_stats)
    lose_line = _cost_row("intraday", lose_stats, 10)
    assert "fee/gross n/a" in lose_line
    assert "fees 1.00% ntl" in lose_line
    win_line = _cost_row("swing", win_stats, 10)
    assert "fee/gross" in win_line and "n/a" not in win_line.split("fee/gross", 1)[1][:10]
    assert "fees 0.50% ntl" in win_line


# ---- MFE capture floor ------------------------------------------------------


def test_mfe_capture_min_floor_excludes_tiny_mfe_and_reports_median():
    assert MFE_CAPTURE_MIN_MFE_R == 0.25
    # MFE_R = 0.10 < 0.25; capture would be (-5/10)/0.10 = -5.0
    tiny = _trade(realized_pnl=-5.0, highest_price=101.0, initial_risk_per_unit=10.0)
    assert trade_mfe_capture(tiny) == pytest.approx(-5.0)
    # Still above epsilon, so other callers see the raw ratio.
    assert trade_mfe_capture(tiny, epsilon=MFE_R_EPSILON) is not None
    kept = _trade(
        ticker="ETH",
        realized_pnl=15.0,
        highest_price=110.0,
        initial_risk_per_unit=10.0,
    )
    mid = _trade(
        ticker="BTC",
        realized_pnl=5.0,
        highest_price=105.0,
        initial_risk_per_unit=10.0,
    )
    stats = aggregate_mfe_capture([tiny, kept, mid])
    assert stats.n == 2
    assert stats.n_excluded == 1
    assert stats.mean == pytest.approx((1.5 + 1.0) / 2.0)
    assert stats.median == pytest.approx(1.25)


# ---- Cross-sleeve overlap ---------------------------------------------------


def test_sol_intraday_115_and_swing_116_overlap_and_non_overlap_pair_does_not():
    window_start = datetime(2026, 10, 4, 0, tzinfo=UTC)
    window_end = datetime(2026, 10, 5, 0, tzinfo=UTC)
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    t115 = _trade(
        id=115,
        ticker="SOL",
        strategy="intraday",
        opened_at=datetime(2026, 10, 4, 3, 49, tzinfo=UTC),
        closed_at=datetime(2026, 10, 4, 7, 49, tzinfo=UTC),
        realized_pnl=-4.25,
        status=TradeStatus.CLOSED,
    )
    t116 = _trade(
        id=116,
        ticker="SOL",
        strategy="swing",
        opened_at=datetime(2026, 10, 4, 4, 5, tzinfo=UTC),
        closed_at=None,
        status=TradeStatus.OPEN,
        realized_pnl=0.0,
    )
    flagged = same_ticker_cross_sleeve_overlaps(
        [t115, t116], window_start=window_start, window_end=window_end, now=now
    )
    assert len(flagged.groups) == 1
    group = flagged.groups[0]
    assert group.ticker == "SOL"
    assert [m.trade_id for m in group.members] == [115, 116]
    assert [m.strategy for m in group.members] == ["intraday", "swing"]
    assert group.members[1].closed_at is None
    assert group.overlap_start == datetime(2026, 10, 4, 4, 5, tzinfo=UTC)
    assert group.overlap_end == datetime(2026, 10, 4, 7, 49, tzinfo=UTC)
    assert group.combined_net_pnl == pytest.approx(-4.25)
    assert flagged.combined_net_pnl == pytest.approx(-4.25)
    line = format_cross_sleeve_overlap_group(group)
    assert "#115 intraday" in line
    assert "#116 swing open" in line
    assert "combined net $-4.25" in line

    closed_early = _trade(
        id=201,
        ticker="ETH",
        strategy="intraday",
        opened_at=datetime(2026, 10, 4, 1, 0, tzinfo=UTC),
        closed_at=datetime(2026, 10, 4, 2, 0, tzinfo=UTC),
        realized_pnl=1.0,
    )
    later_swing = _trade(
        id=202,
        ticker="ETH",
        strategy="swing",
        opened_at=datetime(2026, 10, 4, 3, 0, tzinfo=UTC),
        closed_at=datetime(2026, 10, 4, 4, 0, tzinfo=UTC),
        realized_pnl=2.0,
    )
    empty = same_ticker_cross_sleeve_overlaps(
        [closed_early, later_swing],
        window_start=window_start,
        window_end=window_end,
        now=now,
    )
    assert empty.groups == ()
    assert empty.combined_net_pnl == 0.0

    same_sleeve = _trade(
        id=301,
        ticker="BTC",
        strategy="intraday",
        opened_at=datetime(2026, 10, 4, 5, 0, tzinfo=UTC),
        closed_at=datetime(2026, 10, 4, 8, 0, tzinfo=UTC),
        realized_pnl=1.0,
    )
    same_sleeve_b = _trade(
        id=302,
        ticker="BTC",
        strategy="intraday",
        opened_at=datetime(2026, 10, 4, 6, 0, tzinfo=UTC),
        closed_at=datetime(2026, 10, 4, 7, 0, tzinfo=UTC),
        realized_pnl=1.0,
    )
    assert (
        same_ticker_cross_sleeve_overlaps(
            [same_sleeve, same_sleeve_b],
            window_start=window_start,
            window_end=window_end,
            now=now,
        ).groups
        == ()
    )


# ---- UTC sessions -----------------------------------------------------------


def test_trade_session_uses_utc_boundaries_by_entry():
    assert SESSION_BOUNDARIES_UTC == (
        ("Asia", 0, 8),
        ("Europe", 8, 13),
        ("US", 13, 21),
        ("Late-US", 21, 24),
    )
    cases = (
        (0, "Asia"),
        (7, "Asia"),
        (8, "Europe"),
        (12, "Europe"),
        (13, "US"),
        (20, "US"),
        (21, "Late-US"),
        (23, "Late-US"),
    )
    for hour, name in cases:
        trade = _trade(opened_at=datetime(2026, 10, 4, hour, 30, tzinfo=UTC))
        assert trade_session(trade) == name
    row = format_trade_row(
        _trade(
            opened_at=datetime(2026, 10, 4, 15, 0, tzinfo=UTC),
            closed_at=datetime(2026, 10, 4, 16, tzinfo=UTC),
        )
    )
    assert "session=US" in row


# ---- Entry slippage ---------------------------------------------------------


def test_entry_slippage_snapshot_positive_means_paid_more():
    snap = entry_slippage_snapshot(signal_price=100.0, quote_price=100.5, fill_price=101.0, qty=2.0)
    assert snap["entry_signal_price"] == 100.0
    assert snap["entry_quote_price"] == 100.5
    assert snap["entry_fill_price"] == 101.0
    assert snap["entry_slippage_bps_vs_signal"] == pytest.approx(100.0)
    assert snap["entry_slippage_usd_vs_signal"] == pytest.approx(2.0)
    assert snap["entry_slippage_bps_vs_quote"] == pytest.approx((0.5 / 100.5) * 10_000)
    assert snap["entry_slippage_usd_vs_quote"] == pytest.approx(1.0)
    no_signal = entry_slippage_snapshot(
        signal_price=0.0, quote_price=100.0, fill_price=101.0, qty=1.0
    )
    assert no_signal["entry_signal_price"] is None
    assert "entry_slippage_bps_vs_signal" not in no_signal
    assert no_signal["entry_slippage_bps_vs_quote"] == pytest.approx(100.0)


def test_open_position_writes_entry_slippage_on_open_and_entry_risk(tmp_path):
    class QuoteFillBroker:
        name = "paper"
        server_side_brackets = False

        def current_price(self, _product):
            return 100.0

        def open_long(self, _product, notional, _tp, _sl):
            return Fill("buy", 101.0, notional / 101.0, 0.5)

        def close_long(self, _product, qty, reference_price=None, *, emergency=False):
            return Fill("sell", 101.0, qty, 0.5)

    store = make_store(tmp_path)
    strategy = make_strategy(exit_profile={"label": "slip_open"})
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        QuoteFillBroker(),
        strategies=[strategy],
        config_fingerprint="a" * 64,
    )
    opened = manager.open_position(_candidate(strategy=strategy.name), 1_010.0, strategy)
    assert opened.status == TradeStatus.OPEN
    snap = opened.exit_snapshot
    assert snap["entry_signal_price"] == pytest.approx(100.0)
    assert snap["entry_quote_price"] == pytest.approx(100.0)
    assert snap["entry_fill_price"] == pytest.approx(101.0)
    assert snap["entry_slippage_bps_vs_signal"] == pytest.approx(100.0)
    assert snap["label"] == "slip_open"

    class GapBroker:
        name = "paper"
        server_side_brackets = False

        def current_price(self, _product):
            return 100.0

        def open_long(self, _product, notional, _tp, _sl):
            return Fill("buy", 110.0, notional / 110.0, 1.0)

        def close_long(self, _product, qty, reference_price=None, *, emergency=False):
            return Fill("sell", 109.0, qty, 1.0)

    risk_store = make_store(tmp_path / "risk")
    risk_manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        risk_store,
        GapBroker(),
        strategies=[strategy],
    )
    unwound = risk_manager.open_position(
        _candidate(strategy=strategy.name),
        1_000.0,
        strategy,
        risk_budget_usd=100.0,
    )
    assert unwound.status == TradeStatus.CLOSED
    assert unwound.exit_reason == ExitReason.ENTRY_RISK
    assert unwound.exit_snapshot["entry_fill_price"] == pytest.approx(110.0)
    assert unwound.exit_snapshot["entry_quote_price"] == pytest.approx(100.0)
    assert unwound.exit_snapshot["entry_slippage_bps_vs_quote"] == pytest.approx(1_000.0)


def test_entry_slippage_section_n0_and_forward_stats():
    empty = aggregate_entry_slippage([_trade()])
    assert empty.n == 0
    filled = _trade(
        exit_snapshot={
            "entry_fill_price": 101.0,
            "entry_signal_price": 100.0,
            "entry_quote_price": 100.0,
            "entry_slippage_bps_vs_signal": 100.0,
            "entry_slippage_usd_vs_signal": 1.0,
            "entry_slippage_bps_vs_quote": 80.0,
            "entry_slippage_usd_vs_quote": 0.8,
        }
    )
    other = _trade(
        ticker="ETH",
        exit_snapshot={
            "entry_fill_price": 50.0,
            "entry_slippage_bps_vs_signal": 20.0,
            "entry_slippage_usd_vs_signal": 0.5,
            "entry_slippage_bps_vs_quote": 10.0,
        },
    )
    stats = aggregate_entry_slippage([filled, other, _trade(ticker="BTC")])
    assert stats.n == 2
    assert stats.mean_bps_vs_signal == pytest.approx(60.0)
    assert stats.median_bps_vs_signal == pytest.approx(60.0)
    assert stats.total_usd_vs_signal == pytest.approx(1.5)


def test_backfill_entry_slippage_uses_proposed_entry_price():
    trade = _trade(id=9, entry_price=102.0, original_qty=2.0, qty=2.0)
    stats = backfill_entry_slippage_vs_proposed([trade], {9: 100.0})
    assert stats.n == 1
    assert stats.mean_bps_vs_signal == pytest.approx(200.0)
    assert stats.total_usd_vs_signal == pytest.approx(4.0)


# ---- Reflection clipping ----------------------------------------------------


def test_clip_text_never_cuts_mid_word_and_prefers_sentence_end():
    assert _clip_text("short", 10) == "short"
    long_words = ("abcdefghij " * 50).rstrip()
    clipped = _clip_text(long_words, 40)
    assert clipped.endswith("…")
    assert clipped[-2] != " "
    last_word = clipped[:-1].split()[-1]
    assert last_word == "abcdefghij"
    assert "abcdefg…" not in clipped
    # Sentence end at 70% of limit is kept.
    first = "A" * 70 + ". "
    rest = "B" * 80
    sentence = _clip_text(first + rest, 100)
    assert sentence == "A" * 70 + ".…"
    # Sentence end at 50% of limit falls through to a word boundary.
    early = "Hello world. " + ("word " * 30)
    wordy = _clip_text(early, 100)
    assert wordy.endswith("…")
    assert wordy[:-1].split()[-1] in {"word", "world."}
    assert "wor…" not in wordy


def test_reflection_run_clips_items_at_400_and_states_prompt_limits(tmp_path):
    long_item = ("Alpha token " * 50).strip()
    assert len(long_item) > _ITEM_CHAR_LIMIT

    class RecordingProvider:
        _model_id = "claude-sonnet-test"

        def __init__(self):
            self.instruction = ""

        def complete_json(self, instruction, _payload):
            self.instruction = instruction
            return {
                "summary": "S" * 400,
                "strengths": [long_item],
                "weaknesses": [],
                "recommendations": [],
                "rule_experiments": [],
            }

    provider = RecordingProvider()
    reflector = WeeklyReflector(_cfg_llm(tmp_path), provider=provider)
    assert reflector.request("2026-10-04", {"trades": []})
    reflection = None
    for _ in range(50):
        reflection = reflector.poll()
        if reflection is not None:
            break
        time.sleep(0.01)
    reflector.close()
    assert reflection is not None
    assert f"at most {_PROMPT_ITEM_CHAR_LIMIT} characters" in provider.instruction
    assert f"Summary at most {_SUMMARY_CHAR_LIMIT} characters" in provider.instruction
    assert reflection.strengths
    assert reflection.strengths[0].endswith("…")
    assert "token" in reflection.strengths[0]
    assert not reflection.strengths[0][:-1].endswith(" ")
    last = reflection.strengths[0][:-1].split()[-1]
    assert last == "token" or last.startswith("Alpha")
    assert len(reflection.summary) <= _SUMMARY_CHAR_LIMIT + 1
    assert reflection.summary.endswith("…")


# ---- Synthetic weekly report ------------------------------------------------


def _link_proposed(store, trade, *, price: float, setup_name: str = "breakout_close") -> None:
    trigger_ts = 1_700_000_000 + int(trade.id)
    key = opportunity_key(
        config_fingerprint="b" * 64,
        run_id="i3",
        strategy=trade.strategy,
        ticker=trade.ticker,
        trigger_candle_ts=trigger_ts,
    )
    store.upsert_opportunity(
        opportunity_key=key,
        ledger_version=OPPORTUNITY_LEDGER_VERSION,
        config_fingerprint="b" * 64,
        run_id="i3",
        strategy=trade.strategy,
        ticker=trade.ticker,
        product_id=trade.product_id,
        trigger_granularity_seconds=900,
        trigger_candle_ts=trigger_ts,
        trigger_closed_at=datetime.fromtimestamp(trigger_ts, tz=UTC),
        outcome_status="opened",
        outcome_reason="filled",
        setup_name=setup_name,
        proposed_entry_price=price,
    )
    store.enrich_opportunity(key, trade_id=trade.id)


def test_weekly_report_renders_new_i3_sections(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)
    closed = datetime(2026, 8, 15, 7, 49, tzinfo=UTC)
    sol_intra = Trade(
        ticker="SOL",
        strategy="intraday",
        product_id="SOL-USD",
        qty=1.0,
        original_qty=1.0,
        entry_price=100.0,
        entry_notional=250.0,
        take_profit=110.0,
        stop_loss=95.0,
        highest_price=101.0,
        initial_risk_per_unit=2.0,
        time_stop_at=closed,
        realized_pnl=-4.25,
        fees_paid=1.0,
        status=TradeStatus.CLOSED,
        exit_reason=ExitReason.STOP_LOSS,
        opened_at=datetime(2026, 8, 15, 3, 49, tzinfo=UTC),
        closed_at=closed,
        config_fingerprint="c95a0ad410f4" + "aa",
        exit_snapshot={
            "fee_hurdle_r": 0.6,
            "entry_fill_price": 100.5,
            "entry_signal_price": 100.0,
            "entry_quote_price": 100.2,
            "entry_slippage_bps_vs_signal": 50.0,
            "entry_slippage_usd_vs_signal": 0.5,
            "entry_slippage_bps_vs_quote": 30.0,
            "entry_slippage_usd_vs_quote": 0.3,
        },
    )
    store.add_trade(sol_intra)
    store.add_trade(
        Trade(
            ticker="SOL",
            strategy="swing",
            product_id="SOL-USD",
            qty=1.0,
            original_qty=1.0,
            entry_price=100.0,
            entry_notional=700.0,
            take_profit=110.0,
            stop_loss=95.0,
            highest_price=102.0,
            initial_risk_per_unit=2.0,
            time_stop_at=datetime(2026, 8, 20, tzinfo=UTC),
            realized_pnl=0.0,
            fees_paid=0.0,
            status=TradeStatus.OPEN,
            opened_at=datetime(2026, 8, 15, 4, 5, tzinfo=UTC),
            closed_at=None,
            config_fingerprint="c95a0ad410f4" + "bb",
        )
    )
    eth = Trade(
        ticker="ETH",
        strategy="swing",
        product_id="ETH-USD",
        qty=1.0,
        original_qty=1.0,
        entry_price=100.0,
        entry_notional=400.0,
        take_profit=110.0,
        stop_loss=95.0,
        highest_price=110.0,
        initial_risk_per_unit=10.0,
        time_stop_at=closed,
        realized_pnl=8.0,
        fees_paid=2.0,
        status=TradeStatus.CLOSED,
        exit_reason=ExitReason.TAKE_PROFIT,
        opened_at=datetime(2026, 8, 15, 14, 0, tzinfo=UTC),
        closed_at=datetime(2026, 8, 15, 18, 0, tzinfo=UTC),
        config_fingerprint="c95a0ad410f4" + "cc",
        exit_snapshot={"fee_hurdle_r": 0.6},
    )
    store.add_trade(eth)
    persisted = [t for t in store.closed_trades() if t.ticker == "SOL"][0]
    _link_proposed(store, persisted, price=99.0)

    subject, body = build_weekly_report(store, ["intraday", "swing"], start, end, UTC)
    assert subject
    assert "fee/gross n/a" in body or "fee/gross" in body
    assert "fees " in body and " ntl" in body
    assert "MFE capture (realized_R / MFE_R, only MFE_R >= 0.25R;" in body
    assert "used" in body and "excluded" in body and "median" in body
    assert "Same-ticker cross-sleeve overlap:" in body
    assert "SOL" in body.split("Same-ticker cross-sleeve overlap:", 1)[1]
    assert "swing open" in body
    assert "week combined net of closed members across groups:" in body
    assert "By entry session (UTC, Asia [00:00, 08:00), Europe [08:00, 13:00)," in body
    assert "US [13:00, 21:00), Late-US [21:00, 24:00)):" in body
    assert "session=Asia" in body
    assert "session=US" in body
    assert "Entry slippage (forward-only):" in body
    assert "bps vs signal" in body
    assert "backfill (signal=proposed_entry_price)" in body
    assert "Fee hurdle vs final MFE (this week):" in body
    assert "this week by strategy:" in body
    assert "since gen-8 by strategy:" in body
    assert "intraday:" in body and "swing:" in body

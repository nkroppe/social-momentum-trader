"""Shadow readiness reporting, audit linkage, and read-only CLI behavior."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from _helpers import make_store, make_strategy
from sqlalchemy import inspect, text

from smt.cli import _cmd_shadow_report, build_parser
from smt.config import (
    OpsConfig,
    Settings,
    ShadowReportConfig,
    SignalsConfig,
    SourcesConfig,
    TierConfig,
    UniverseConfig,
    XSource,
)
from smt.llm.config import LLMConfig
from smt.models import SocialCount, Trade, TradeStatus
from smt.ops.shadow_report import analyze_shadow_readiness, build_shadow_report
from smt.run import Runner
from smt.store import Store
from smt.trader.risk import RiskDecision
from smt.trader.signals import TradeCandidate

END = datetime(2026, 8, 9, 12, tzinfo=UTC)
START = END - timedelta(hours=1)


def _universe(*, include_major: bool = False) -> UniverseConfig:
    symbols = {
        "SOL": {"product_id": "SOL-USD", "aliases": ["$sol"], "tier": "large"}
    }
    if include_major:
        symbols["BTC"] = {
            "product_id": "BTC-USD",
            "aliases": ["$btc"],
            "tier": "major",
        }
    return UniverseConfig(symbols=symbols)


def _sources(*, include_major: bool = False) -> SourcesConfig:
    keywords = ["$SOL", "$BTC"] if include_major else ["$SOL"]
    return SourcesConfig(
        x=XSource(
            enabled=True,
            keywords=keywords,
            counts_enabled=True,
            count_window_minutes=30,
        )
    )


def _signals() -> SignalsConfig:
    return SignalsConfig(
        social_decision_mode="shadow",
        tiers={
            "large": TierConfig(social_policy="optional"),
            "major": TierConfig(social_policy="ignored"),
        },
    )


def _llm() -> LLMConfig:
    return LLMConfig(
        judge={
            "tiers": ["large"],
            "required_tiers": [],
            "min_catalyst_score": 0.55,
        }
    )


def _cfg() -> ShadowReportConfig:
    return ShadowReportConfig(
        report_days=1,
        min_observation_days=1,
        min_count_coverage=1.0,
        min_closed_linked_trades_per_tier=2,
        min_completed_per_outcome_group=1,
        max_llm_error_rate=0.05,
        min_expectancy_separation_r=0.25,
    )


def _seed_full_counts(store: Store, ticker: str = "SOL") -> None:
    store.add_social_counts(
        [
            SocialCount(
                source="x",
                ticker=ticker,
                query=f"${ticker}",
                tweet_count=5,
                window_start=START + timedelta(minutes=30 * index),
                window_end=START + timedelta(minutes=30 * (index + 1)),
            )
            for index in range(2)
        ]
    )


def _trade(store: Store, pnl: float, suffix: str) -> Trade:
    return store.add_trade(
        Trade(
            ticker="SOL",
            strategy="intraday",
            product_id="SOL-USD",
            status=TradeStatus.CLOSED,
            qty=1,
            original_qty=1,
            entry_price=100,
            entry_notional=100,
            take_profit=110,
            stop_loss=90,
            initial_risk_per_unit=10,
            time_stop_at=END,
            realized_pnl=pnl,
            broker_entry_order_id=suffix,
            opened_at=START + timedelta(minutes=5),
            closed_at=END - timedelta(minutes=5),
        )
    )


def _decision(
    store: Store,
    key: str,
    trade: Trade | None,
    *,
    tier: str = "large",
    social: str,
    llm_status: str = "complete",
    llm_score: float = 0.8,
    llm_veto: bool = False,
) -> None:
    store.upsert_shadow_decision(
        decision_key=key,
        trade_id=trade.id if trade else 0,
        ticker="SOL" if tier != "major" else "BTC",
        strategy="intraday",
        tier=tier,
        decision_mode="shadow",
        setup="breakout",
        social_decision=social,
        llm_status=llm_status,
        llm_score=llm_score,
        llm_veto=llm_veto,
        risk_status="approved",
        first_evaluated_at=START + timedelta(minutes=10),
    )


def test_migration_adds_indexed_trade_id(tmp_path):
    store = Store(f"sqlite:///{tmp_path}/legacy.sqlite")
    with store.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE shadow_decisions ("
                "id INTEGER PRIMARY KEY, decision_key VARCHAR(64) UNIQUE)"
            )
        )
    store.init_db()
    columns = {
        column["name"] for column in inspect(store.engine).get_columns("shadow_decisions")
    }
    indexes = {
        index["name"] for index in inspect(store.engine).get_indexes("shadow_decisions")
    }
    assert "trade_id" in columns
    assert "ix_shadow_decisions_trade_id" in indexes


def test_runner_links_trade_and_later_audit_preserves_link(tmp_path):
    store = make_store(tmp_path)
    trade = _trade(store, 10, "linked")
    candidate = TradeCandidate(
        "SOL",
        "SOL-USD",
        5,
        20,
        1,
        "test",
        tier="large",
        strategy="intraday",
        setup="breakout",
        setup_metadata={"trigger_ts": "2026-08-09T11:00:00Z"},
        social_decision="would_pass",
    )
    strategy = make_strategy()
    runner = Runner.__new__(Runner)
    runner.store = store
    runner.strategies = [strategy]
    runner.manager = SimpleNamespace(
        allocation_start_equity=lambda _st: 2500,
        allocation_equity=lambda _st: 2500,
        open_position=MagicMock(return_value=trade),
    )
    runner.risk_gate = SimpleNamespace(
        portfolio_halted=lambda *_args: (False, ""),
        evaluate=lambda *_args: RiskDecision(True, 100, "approved"),
    )
    runner.scorers = {"intraday": SimpleNamespace(score_all=lambda: [])}
    runner.signal_engines = {
        "intraday": SimpleNamespace(candidates=lambda _scores: [candidate])
    }
    runner.llm = SimpleNamespace(review_candidate=lambda _candidate: True)
    runner._halt_notified = set()
    runner.evaluate_and_trade()
    linked = store.shadow_decision(candidate.decision_key)
    assert linked.trade_id == trade.id
    assert linked.risk_status == "approved"
    runner._audit_candidate(
        candidate,
        risk_status="rejected",
        risk_reason="already have an open position for this ticker",
    )
    preserved = store.shadow_decision(candidate.decision_key)
    assert preserved.trade_id == trade.id
    assert preserved.risk_status == "approved"
    assert preserved.risk_reason == "approved"


def test_no_data_report_is_usefully_not_ready(tmp_path):
    store = make_store(tmp_path)
    subject, body = build_shadow_report(
        store, _cfg(), _sources(), _signals(), _universe(), _llm(), START, END
    )
    assert "SOCIAL NOT READY" in subject
    assert "L3 NOT READY" in subject
    assert "0/2 (0.0%)" in body
    assert "No statistical certainty" not in body
    assert "does not claim statistical certainty" in body
    assert len(body) < 4096


def test_count_coverage_uses_exact_distinct_windows_and_missing_math(tmp_path):
    store = make_store(tmp_path)
    _seed_full_counts(store)
    # Idempotent duplicate does not inflate observed coverage.
    _seed_full_counts(store)
    full = store.count_coverage("SOL", START, END, 30)
    missing = store.count_coverage("BTC", START, END, 30)
    assert (full.observed, full.expected, full.ratio) == (2, 2, 1.0)
    assert (missing.observed, missing.expected, missing.ratio) == (0, 2, 0.0)


def test_net_r_groups_and_profitable_false_rejects(tmp_path):
    store = make_store(tmp_path)
    _seed_full_counts(store)
    winner = _trade(store, 10, "pass")
    false_reject = _trade(store, 5, "reject")
    _decision(store, "pass", winner, social="would_boost")
    _decision(store, "reject", false_reject, social="would_reject", llm_veto=True)
    summary = analyze_shadow_readiness(
        store, _cfg(), _sources(), _signals(), _universe(), _llm(), START, END
    )
    large = summary.social_tiers["large"]
    assert large.passed.average_r == pytest.approx(1.0)
    assert large.rejected.average_r == pytest.approx(0.5)
    assert large.rejected.profitable_rejects == 1
    assert large.rejected.profitable_reject_pnl == 5


def test_outcome_floor_requires_linked_closed_not_raw_decisions(tmp_path):
    store = make_store(tmp_path)
    _seed_full_counts(store)
    pass_trade = _trade(store, 10, "pass")
    reject_trade = _trade(store, -10, "reject")
    _decision(store, "pass-linked", pass_trade, social="would_pass")
    _decision(store, "pass-unlinked", None, social="would_pass")
    _decision(
        store, "reject-linked", reject_trade, social="would_reject", llm_veto=True
    )
    _decision(
        store, "reject-unlinked", None, social="would_reject", llm_veto=True
    )
    cfg = _cfg().model_copy(update={"min_completed_per_outcome_group": 2})
    summary = analyze_shadow_readiness(
        store, cfg, _sources(), _signals(), _universe(), _llm(), START, END
    )
    reasons = summary.social_tiers["large"].reasons
    assert any("pass group has 1 linked closed outcomes < 2" in reason for reason in reasons)
    assert any("reject group has 1 linked closed outcomes < 2" in reason for reason in reasons)
    assert not summary.social_ready


def test_pending_heavy_sonnet_tier_fails_completion_rate(tmp_path):
    store = make_store(tmp_path)
    _seed_full_counts(store)
    win = _trade(store, 10, "win")
    loss = _trade(store, -10, "loss")
    _decision(store, "complete-pass", win, social="would_pass")
    _decision(
        store,
        "complete-reject",
        loss,
        social="would_reject",
        llm_veto=True,
    )
    for index in range(18):
        _decision(
            store,
            f"pending-{index}",
            None,
            social="would_pass",
            llm_status="pending",
        )
    summary = analyze_shadow_readiness(
        store, _cfg(), _sources(), _signals(), _universe(), _llm(), START, END
    )
    assert not summary.sonnet_ready
    assert any(
        "LLM completion rate 10.0% < 95.0%" in reason
        for reason in summary.sonnet_tiers["large"].reasons
    )


def test_disabled_current_pipelines_fail_closed_despite_history(tmp_path):
    store = make_store(tmp_path)
    _seed_full_counts(store)
    win = _trade(store, 10, "win")
    loss = _trade(store, -10, "loss")
    _decision(store, "pass", win, social="would_pass")
    _decision(store, "reject", loss, social="would_reject", llm_veto=True)

    disabled_sources = _sources()
    disabled_sources.x.enabled = False
    disabled_sources.x.counts_enabled = False
    social = analyze_shadow_readiness(
        store,
        _cfg(),
        disabled_sources,
        _signals(),
        _universe(),
        _llm(),
        START,
        END,
    )
    assert not social.social_ready
    assert any("X source is disabled" in reason for reason in social.social_reasons)

    disabled_llm = _llm().model_copy(deep=True)
    disabled_llm.enabled = False
    disabled_llm.judge.enabled = False
    sonnet = analyze_shadow_readiness(
        store,
        _cfg(),
        _sources(),
        _signals(),
        _universe(),
        disabled_llm,
        START,
        END,
    )
    assert not sonnet.sonnet_ready
    assert any("LLM is disabled" in reason for reason in sonnet.sonnet_reasons)


def test_social_and_sonnet_readiness_are_independent(tmp_path):
    store = make_store(tmp_path)
    _seed_full_counts(store)
    win = _trade(store, 10, "win")
    loss = _trade(store, -10, "loss")
    _decision(store, "pass", win, social="would_pass", llm_status="pending")
    _decision(store, "reject", loss, social="would_reject", llm_status="pending")
    summary = analyze_shadow_readiness(
        store, _cfg(), _sources(), _signals(), _universe(), _llm(), START, END
    )
    assert summary.social_ready
    assert not summary.sonnet_ready

    store2 = make_store(tmp_path / "sonnet")
    _seed_full_counts(store2)
    win2 = _trade(store2, 10, "win")
    loss2 = _trade(store2, -10, "loss")
    _decision(store2, "pass", win2, social="would_pass", llm_score=0.8)
    _decision(
        store2,
        "reject",
        loss2,
        social="would_pass",
        llm_score=0.1,
        llm_veto=True,
    )
    summary2 = analyze_shadow_readiness(
        store2, _cfg(), _sources(), _signals(), _universe(), _llm(), START, END
    )
    assert not summary2.social_ready
    assert summary2.sonnet_ready


def test_optional_pass_semantics_and_ignored_major_do_not_block(tmp_path):
    store = make_store(tmp_path)
    _seed_full_counts(store)
    _seed_full_counts(store, "BTC")
    win = _trade(store, 10, "win")
    loss = _trade(store, -10, "loss")
    _decision(store, "plain-pass", win, social="would_pass")
    _decision(store, "reject", loss, social="would_reject", llm_veto=True)
    _decision(store, "major", None, tier="major", social="ignored", llm_status="bypassed")
    summary = analyze_shadow_readiness(
        store,
        _cfg(),
        _sources(include_major=True),
        _signals(),
        _universe(include_major=True),
        _llm(),
        START,
        END,
    )
    assert "major" not in summary.social_tiers
    assert summary.social_tiers["large"].passed.decisions == 1
    assert summary.social_ready
    _, body = build_shadow_report(
        store,
        _cfg(),
        _sources(include_major=True),
        _signals(),
        _universe(include_major=True),
        _llm(),
        START,
        END,
    )
    assert "bypassed=0" in body


def test_shadow_report_cli_is_read_only_and_send_failure_is_nonzero(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path}/cli.sqlite")
    patches = (
        patch("smt.config.get_settings", return_value=settings),
        patch("smt.config.get_ops", return_value=OpsConfig(shadow_report=_cfg())),
        patch("smt.config.get_sources", return_value=_sources()),
        patch("smt.config.get_signals", return_value=_signals()),
        patch("smt.config.get_universe", return_value=_universe()),
        patch("smt.llm.get_llm", return_value=_llm()),
    )
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch("smt.run.Runner") as runner,
        patch("httpx.Client") as client,
    ):
        assert _cmd_shadow_report(argparse.Namespace(days=1, send=False)) == 0
    runner.assert_not_called()
    client.assert_not_called()

    with (
        patch("smt.config.get_settings", return_value=settings),
        patch("smt.config.get_ops", return_value=OpsConfig(shadow_report=_cfg())),
        patch("smt.config.get_sources", return_value=_sources()),
        patch("smt.config.get_signals", return_value=_signals()),
        patch("smt.config.get_universe", return_value=_universe()),
        patch("smt.llm.get_llm", return_value=_llm()),
        patch("smt.ops.Alerter.notify", return_value=False),
    ):
        assert _cmd_shadow_report(argparse.Namespace(days=1, send=True)) == 1


def test_shadow_report_cli_rejects_nonpositive_days():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["shadow-report", "--days", "0"])

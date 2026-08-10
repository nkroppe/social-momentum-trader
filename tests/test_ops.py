"""Tests for soak tracking and preflight checks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from smt.config import (
    get_market,
    get_ops,
    get_risk,
    get_signals,
    get_sources,
    get_strategies,
    get_universe,
)
from smt.llm import get_llm
from smt.ops.preflight import all_passed, run_preflight
from smt.ops.soak import MAX_PRIOR_GENERATIONS, SoakTracker
from smt.policy import trading_policy_identity


def _policy(**overrides):
    values = {
        "strategies": get_strategies(),
        "risk": get_risk(),
        "market": get_market(),
        "signals": get_signals(),
        "universe": get_universe(),
        "sources": get_sources(),
        "llm": get_llm(),
    }
    values.update(overrides)
    return trading_policy_identity(**values)


def test_soak_tracker_starts_and_measures_days(tmp_path):
    tracker = SoakTracker(tmp_path / "soak.json")
    tracker.ensure_started("paper")
    assert tracker.days_elapsed() >= 0.0

    # Backdate start to 15 days ago.
    old = datetime.now(UTC) - timedelta(days=15)
    (tmp_path / "soak.json").write_text(
        f'{{"started_at": "{old.isoformat()}", "mode": "paper"}}', encoding="utf-8"
    )
    tracker = SoakTracker(tmp_path / "soak.json")
    assert tracker.meets_minimum(14)
    assert "READY" in tracker.summary_line(14)


def test_trading_policy_fingerprint_is_deterministic_sha256():
    first = _policy()
    second = _policy()

    assert first == second
    assert len(first.fingerprint) == 64
    assert all(char in "0123456789abcdef" for char in first.fingerprint)
    assert set(first.manifest) == {
        "schema",
        "strategies",
        "risk",
        "market",
        "signals",
        "universe",
        "sources",
        "llm",
    }


def test_relevant_policy_change_starts_new_soak_generation(tmp_path):
    first = _policy()
    changed_risk = get_risk().model_copy(
        update={"risk_per_trade_pct": get_risk().risk_per_trade_pct / 2}
    )
    second = _policy(risk=changed_risk)
    tracker = SoakTracker(tmp_path / "soak.json")

    original = tracker.ensure_started(
        fingerprint=first.fingerprint,
        manifest=first.manifest,
    )
    changed = tracker.ensure_started(
        fingerprint=second.fingerprint,
        manifest=second.manifest,
    )

    assert changed.generation == original.generation + 1
    assert changed.active_fingerprint == second.fingerprint
    assert changed.changed_sections == ["risk"]
    assert changed.invalidation_reason == "trading-policy fingerprint changed"
    assert len(changed.history or []) == 1


def test_irrelevant_paths_reflection_and_secrets_do_not_change_policy(
    monkeypatch,
):
    baseline = _policy()
    irrelevant = get_llm().model_copy(deep=True)
    irrelevant.request_timeout_seconds += 30
    irrelevant.budget_state_file = "/secret/runtime/budget.json"
    irrelevant.sandbox_dir = "/secret/runtime/sandbox"
    irrelevant.judge.state_file = "/secret/runtime/judge.json"
    irrelevant.reflection.state_file = "/secret/runtime/reflections.jsonl"
    irrelevant.reflection.deliver_telegram = not irrelevant.reflection.deliver_telegram
    monkeypatch.setenv("SMTP_PASSWORD", "must-not-be-hashed")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-be-hashed")

    assert _policy(llm=irrelevant) == baseline


def test_legacy_soak_state_invalidates_once(tmp_path):
    path = tmp_path / "soak.json"
    old = datetime.now(UTC) - timedelta(days=30)
    path.write_text(
        json.dumps({"started_at": old.isoformat(), "mode": "paper"}),
        encoding="utf-8",
    )
    policy = _policy()
    tracker = SoakTracker(path)

    migrated = tracker.ensure_started(
        fingerprint=policy.fingerprint,
        manifest=policy.manifest,
    )
    unchanged = tracker.ensure_started(
        fingerprint=policy.fingerprint,
        manifest=policy.manifest,
    )

    assert migrated.generation == 1
    assert migrated.changed_sections == ["legacy_state"]
    assert migrated.invalidation_reason == "legacy state had no trading-policy fingerprint"
    assert len(migrated.history or []) == 1
    assert unchanged.started_at == migrated.started_at
    assert len(unchanged.history or []) == 1


def test_soak_generation_history_is_bounded(tmp_path):
    tracker = SoakTracker(tmp_path / "soak.json")
    policy = _policy()
    state = tracker.ensure_started(
        fingerprint=policy.fingerprint,
        manifest=policy.manifest,
    )
    for index in range(MAX_PRIOR_GENERATIONS + 4):
        state = tracker.restart(
            fingerprint=f"{index:064x}",
            manifest={"risk": f"{index:012x}"},
            reason=f"test reset {index}",
        )

    assert state.generation == MAX_PRIOR_GENERATIONS + 5
    assert len(state.history or []) == MAX_PRIOR_GENERATIONS
    assert (state.history or [])[0].generation == 5


def test_explicit_soak_reset_records_reason_without_policy_change(tmp_path):
    tracker = SoakTracker(tmp_path / "soak.json")
    policy = _policy()
    tracker.ensure_started(
        fingerprint=policy.fingerprint,
        manifest=policy.manifest,
    )

    state = tracker.restart(
        fingerprint=policy.fingerprint,
        manifest=policy.manifest,
        reason="explicit soak-reset command",
    )

    assert state.generation == 2
    assert state.invalidation_reason == "explicit soak-reset command"
    assert state.changed_sections == []
    assert (state.history or [])[0].invalidation_reason == "explicit soak-reset command"


def test_preflight_dev_passes():
    results = run_preflight("dev")
    assert all_passed(results)


def test_preflight_production_flags_missing_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("smt.ops.preflight._market_data_checks", lambda: [])
    results = {r.name: r for r in run_preflight("production")}
    assert results["mock_disabled"].passed is not get_sources().mock.enabled
    assert results["alert_channel"].passed is False  # no alerts configured
    assert results[".env file"].passed is False  # cwd has no .env


def test_live_preflight_requires_soak(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("smt.ops.preflight._market_data_checks", lambda: [])
    results = {r.name: r for r in run_preflight("live")}
    assert results["live_flag"].passed is False
    assert results["paper_soak_duration"].passed is False


def test_live_preflight_fails_closed_on_policy_mismatch(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("smt.ops.preflight._market_data_checks", lambda: [])
    ops = get_ops().model_copy(deep=True)
    ops.soak.state_file = str(tmp_path / "soak.json")
    monkeypatch.setattr("smt.ops.preflight.get_ops", lambda: ops)
    old = datetime.now(UTC) - timedelta(days=30)
    (tmp_path / "soak.json").write_text(
        json.dumps(
            {
                "started_at": old.isoformat(),
                "mode": "paper",
                "active_fingerprint": "f" * 64,
                "manifest": {"risk": "old"},
                "generation": 7,
            }
        ),
        encoding="utf-8",
    )

    results = {result.name: result for result in run_preflight("live")}

    assert results["soak_policy_generation"].passed is False
    assert "generation=7" in results["soak_policy_generation"].detail
    assert results["paper_soak_duration"].passed is False
    assert "fingerprint mismatch" in results["paper_soak_duration"].detail

"""Tests for soak tracking and preflight checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from smt.config import get_sources
from smt.ops.preflight import all_passed, run_preflight
from smt.ops.soak import SoakTracker


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


def test_preflight_dev_passes():
    results = run_preflight("dev")
    assert all_passed(results)


def test_preflight_production_flags_missing_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    results = {r.name: r for r in run_preflight("production")}
    assert results["mock_disabled"].passed is not get_sources().mock.enabled
    assert results["alert_channel"].passed is False  # no alerts configured
    assert results[".env file"].passed is False  # cwd has no .env


def test_live_preflight_requires_soak(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    results = {r.name: r for r in run_preflight("live")}
    assert results["live_flag"].passed is False
    assert results["paper_soak_duration"].passed is False

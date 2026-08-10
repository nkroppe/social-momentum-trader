"""X read-budget accounting: dedupe-aware billing and daily pacing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from smt.ingest.x import BudgetStateUnavailable, ReadBudget


def _budget(tmp_path, limit=20_000) -> ReadBudget:
    return ReadBudget(tmp_path / "x_budget.json", limit)


AUG_8 = datetime(2026, 8, 8, 12, tzinfo=UTC)
AUG_9 = datetime(2026, 8, 9, 12, tzinfo=UTC)


# ---- Dedupe-aware counting --------------------------------------------------


def test_only_new_posts_are_billed(tmp_path):
    """X dedupes within a UTC day, so overlapping polls must not double-count."""
    b = _budget(tmp_path)
    assert b.register(["1", "2", "3"], now=AUG_8) == 3
    # Next poll re-returns two of them plus one new.
    assert b.register(["2", "3", "4"], now=AUG_8) == 1
    assert b.reads_used == 4


def test_the_dedupe_window_resets_on_a_new_utc_day(tmp_path):
    b = _budget(tmp_path)
    b.register(["1", "2"], now=AUG_8)
    # Same posts tomorrow are billable again.
    assert b.register(["1", "2"], now=AUG_9) == 2
    assert b.reads_used == 4


def test_daily_counters_reset_but_monthly_total_carries(tmp_path):
    b = _budget(tmp_path)
    b.register(["1", "2", "3"], now=AUG_8)
    assert b.day_used(AUG_8) == 3
    assert b.day_used(AUG_9) == 0
    assert b.reads_used == 3


def test_empty_results_cost_nothing(tmp_path):
    b = _budget(tmp_path)
    assert b.register([], now=AUG_8) == 0
    assert b.reads_used == 0


# ---- Daily pacing -----------------------------------------------------------


def test_allowance_spreads_the_remaining_budget_over_days_left(tmp_path):
    b = _budget(tmp_path, limit=24_000)
    # Aug 8 of a 31-day month leaves 24 days including today.
    assert b.daily_allowance(AUG_8) == 1_000


def test_underspending_raises_the_later_allowance(tmp_path):
    """Pacing is self-correcting rather than a fixed daily slice."""
    b = _budget(tmp_path, limit=24_000)
    before = b.daily_allowance(AUG_8)
    # Spend nothing on the 8th; on the 9th, 23 days remain for the full budget.
    assert b.daily_allowance(AUG_9) > before


def test_day_remaining_falls_as_the_day_is_spent(tmp_path):
    b = _budget(tmp_path, limit=24_000)
    b.register([str(i) for i in range(400)], now=AUG_8)
    assert b.day_used(AUG_8) == 400
    assert b.day_remaining(AUG_8) == 600


def test_pacing_stops_a_single_day_draining_the_month(tmp_path):
    b = _budget(tmp_path, limit=24_000)
    b.register([str(i) for i in range(1_000)], now=AUG_8)
    assert b.day_remaining(AUG_8) == 0
    # The month is barely touched, so tomorrow still has room.
    assert b.remaining == 23_000
    assert b.day_remaining(AUG_9) > 0


# ---- Reporting --------------------------------------------------------------


def test_spend_is_reported_in_dollars(tmp_path):
    b = ReadBudget(tmp_path / "x_budget.json", 20_000, cost_per_read_usd=0.005)
    b.register([str(i) for i in range(200)], now=AUG_8)
    assert b.spend_usd == 1.0
    assert b.budget_usd == 100.0


def test_first_registration_records_the_start_of_polling(tmp_path):
    b = _budget(tmp_path)
    assert b.started_at is None
    b.register(["1"], now=AUG_8)
    assert b.started_at == AUG_8


def test_start_time_is_not_pushed_forward_by_later_reads(tmp_path):
    """The rate denominator must span all polling, not just the latest poll."""
    b = _budget(tmp_path)
    b.register(["1"], now=AUG_8)
    b.register(["2"], now=AUG_8 + timedelta(hours=2))
    assert b.started_at == AUG_8


# ---- Resilience -------------------------------------------------------------


def test_a_new_month_seeds_opening_reads_from_the_console(tmp_path):
    path = tmp_path / "x_budget.json"
    old = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m")
    path.write_text(
        json.dumps({"month": old, "reads": 19_000, "started_at": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    b = ReadBudget(path, 20_000, opening_reads=500)
    assert b.reads_used == 500
    assert b.started_at is not None


def test_a_new_month_resets_usage_and_the_clock(tmp_path):
    path = tmp_path / "x_budget.json"
    path.write_text(
        json.dumps(
            {
                "month": "2026-07",
                "reads": 19_000,
                "started_at": "2026-07-01T00:00:00+00:00",
                "day": "2026-07-31",
                "day_reads": 500,
                "day_ids": ["1"],
            }
        ),
        encoding="utf-8",
    )
    b = ReadBudget(path, 20_000, opening_reads=0)
    assert b.reads_used == 0
    assert b.started_at is None
    assert b.register(["1"], now=AUG_8) == 1


def test_corrupt_state_fails_closed(tmp_path):
    path = tmp_path / "x_budget.json"
    path.write_text("{not json", encoding="utf-8")
    b = ReadBudget(path, 20_000)
    with pytest.raises(BudgetStateUnavailable, match="refusing paid requests"):
        _ = b.reads_used
    with pytest.raises(BudgetStateUnavailable):
        b.register(["1"], now=AUG_8)


def test_legacy_state_without_daily_fields_is_upgraded(tmp_path):
    """Existing deployments carry a file written before dedupe tracking."""
    path = tmp_path / "x_budget.json"
    month = AUG_8.strftime("%Y-%m")
    path.write_text(json.dumps({"month": month, "reads": 1_200}), encoding="utf-8")
    b = ReadBudget(path, 20_000)
    assert b.reads_used == 1_200
    assert b.register(["1", "2"], now=AUG_8) == 2
    assert b.reads_used == 1_202

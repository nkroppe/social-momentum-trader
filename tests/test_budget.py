"""X read-budget accounting and burn-rate measurement."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from smt.ingest.x import ReadBudget


def _budget(tmp_path, limit=50_000) -> ReadBudget:
    return ReadBudget(tmp_path / "x_budget.json", limit)


def test_consume_accumulates_and_reduces_remaining(tmp_path):
    b = _budget(tmp_path)
    b.consume(100)
    b.consume(50)
    assert b.reads_used == 150
    assert b.remaining == 49_850


def test_first_consume_records_the_start_of_polling(tmp_path):
    b = _budget(tmp_path)
    assert b.started_at is None
    b.consume(10)
    assert b.started_at is not None
    assert (datetime.now(UTC) - b.started_at).total_seconds() < 60


def test_start_time_is_not_pushed_forward_by_later_reads(tmp_path):
    """The rate denominator must span all polling, not just the latest poll."""
    b = _budget(tmp_path)
    b.consume(10)
    first = b.started_at
    b.consume(10)
    assert b.started_at == first


def test_a_new_month_resets_usage_and_the_clock(tmp_path):
    path = tmp_path / "x_budget.json"
    old = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m")
    path.write_text(
        json.dumps({"month": old, "reads": 49_000, "started_at": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    b = ReadBudget(path, 50_000)
    assert b.reads_used == 0
    assert b.started_at is None


def test_corrupt_state_does_not_crash_ingest(tmp_path):
    path = tmp_path / "x_budget.json"
    path.write_text("{not json", encoding="utf-8")
    b = ReadBudget(path, 50_000)
    assert b.reads_used == 0
    assert b.started_at is None

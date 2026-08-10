"""Sparse LLM judge/reflection behavior without external model calls."""

from __future__ import annotations

import json
import threading
import time

import pytest

from smt.llm.config import LLMConfig
from smt.llm.judge import SparseL3Judge, safe_judge_context
from smt.llm.provider import LLMBudgetExhausted, MonthlyCallBudget, parse_json_response
from smt.llm.reflection import WeeklyReflector


class FakeProvider:
    _model_id = "claude-sonnet-test"

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def complete_json(self, _instruction, _payload):
        self.calls += 1
        return self.response


def _cfg(tmp_path, **overrides) -> LLMConfig:
    data = {
        "budget_state_file": str(tmp_path / "budget.json"),
        "sandbox_dir": str(tmp_path / "sandbox"),
        "judge": {"state_file": str(tmp_path / "judge.json")},
        "reflection": {"state_file": str(tmp_path / "reflections.jsonl")},
    }
    data.update(overrides)
    return LLMConfig(**data)


def _wait_for_judge(judge, context):
    for _ in range(50):
        decision = judge.evaluate(context)
        if not decision.pending:
            return decision
        time.sleep(0.01)
    raise AssertionError("judge did not complete")


def test_json_parser_accepts_plain_or_fenced_json():
    assert parse_json_response('{"veto":false}') == {"veto": False}
    assert parse_json_response('```json\n{"veto":true}\n```') == {"veto": True}


def test_monthly_call_budget_is_hard_capped(tmp_path):
    budget = MonthlyCallBudget(str(tmp_path / "budget.json"), 2)
    budget.reserve()
    budget.reserve()
    with pytest.raises(LLMBudgetExhausted):
        budget.reserve()


def test_monthly_call_budget_fails_closed_on_corrupt_state(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text("{not-json", encoding="utf-8")
    budget = MonthlyCallBudget(str(path), 10)
    with pytest.raises(LLMBudgetExhausted, match="unreadable"):
        budget.reserve()


def test_judge_is_sparse_non_blocking_and_cached(tmp_path):
    cfg = _cfg(tmp_path)
    provider = FakeProvider(
        {
            "veto": False,
            "catalyst_score": 0.81,
            "confidence": 0.75,
            "narrative": "credible protocol catalyst",
            "reason": "independent authors discuss a concrete event",
        }
    )
    judge = SparseL3Judge(cfg, provider=provider)
    context = {
        "ticker": "PUMP",
        "strategy": "swing",
        "tier": "micro",
        "technical_setup": {"setup": "breakout_retest", "volume_ratio": 2.2},
        "recent_social_posts": [{"text": "concrete launch event"}],
    }

    first = judge.evaluate(context)
    assert first.pending
    complete = _wait_for_judge(judge, context)
    assert complete.approved and not complete.veto
    assert provider.calls == 1
    assert judge.evaluate(context) == complete
    assert provider.calls == 1
    judge.close()


def test_new_social_context_invalidates_cached_approval(tmp_path):
    cfg = _cfg(tmp_path)
    provider = FakeProvider(
        {
            "veto": False,
            "catalyst_score": 0.8,
            "confidence": 0.8,
            "narrative": "valid",
            "reason": "valid",
        }
    )
    judge = SparseL3Judge(cfg, provider=provider)
    context = {
        "ticker": "PUMP",
        "strategy": "swing",
        "tier": "micro",
        "setup_id": "PUMP:swing:123",
        "recent_social_posts": [{"text": "launch", "created_at": "2026-08-09T01:00:00Z"}],
    }
    assert _wait_for_judge(judge, context).approved
    changed = {
        **context,
        "recent_social_posts": [
            {"text": "credible exploit report", "created_at": "2026-08-09T01:10:00Z"}
        ],
    }
    assert judge.evaluate(changed).pending
    assert _wait_for_judge(judge, changed).approved
    assert provider.calls == 2
    judge.close()


def test_new_social_context_does_not_queue_behind_stale_jobs_unbounded(tmp_path):
    started = threading.Event()
    release = threading.Event()

    class BlockingProvider(FakeProvider):
        def complete_json(self, instruction, payload):
            self.calls += 1
            started.set()
            release.wait(2)
            return self.response

    provider = BlockingProvider(
        {
            "veto": False,
            "catalyst_score": 0.8,
            "confidence": 0.8,
            "narrative": "valid",
            "reason": "valid",
        }
    )
    judge = SparseL3Judge(_cfg(tmp_path), provider=provider)
    first = {
        "ticker": "CAP",
        "strategy": "swing",
        "tier": "micro",
        "setup_id": "CAP:swing:123",
        "recent_social_posts": [{"text": "first"}],
    }
    changed = {**first, "recent_social_posts": [{"text": "new adverse context"}]}
    assert judge.evaluate(first).pending
    assert started.wait(1)
    assert judge.evaluate(changed).pending
    # At most one running stale call plus the newest queued context.
    assert len(judge._pending) == 2
    assert provider.calls == 1
    release.set()
    assert _wait_for_judge(judge, changed).approved
    assert provider.calls == 2
    judge.close()


def test_poll_harvests_latest_context_and_suppresses_stale_result(tmp_path):
    started = threading.Event()
    release = threading.Event()

    class ContextProvider(FakeProvider):
        def complete_json(self, _instruction, payload):
            self.calls += 1
            text = payload["recent_social_posts"][0]["text"]
            if text == "old":
                started.set()
                release.wait(2)
            return {
                "veto": text == "new",
                "catalyst_score": 0.1 if text == "new" else 0.9,
                "confidence": 0.9,
                "narrative": text,
                "reason": text,
            }

    provider = ContextProvider({})
    judge = SparseL3Judge(_cfg(tmp_path), provider=provider)
    first = {
        "ticker": "CAP",
        "strategy": "swing",
        "tier": "micro",
        "setup_id": "CAP:swing:123",
        "recent_social_posts": [{"text": "old"}],
    }
    changed = {**first, "recent_social_posts": [{"text": "new"}]}
    old_pending = judge.evaluate(first)
    assert old_pending.pending and started.wait(1)
    new_pending = judge.evaluate(changed)
    assert new_pending.pending
    release.set()

    completed = []
    for _ in range(100):
        completed.extend(judge.poll_completed())
        if completed:
            break
        time.sleep(0.01)
    assert [decision.key for decision in completed] == [new_pending.key]
    assert completed[0].veto is True
    assert completed[0].narrative == "new"
    judge.close()


def test_required_tier_rejects_weak_catalyst(tmp_path):
    cfg = _cfg(tmp_path)
    provider = FakeProvider(
        {
            "veto": False,
            "catalyst_score": 0.2,
            "confidence": 0.8,
            "narrative": "generic chatter",
            "reason": "no concrete catalyst",
        }
    )
    judge = SparseL3Judge(cfg, provider=provider)
    context = {"ticker": "CAP", "strategy": "intraday", "tier": "micro"}
    decision = _wait_for_judge(judge, context)
    assert not decision.approved
    assert not decision.veto
    judge.close()


def test_major_tier_bypasses_llm(tmp_path):
    provider = FakeProvider({})
    judge = SparseL3Judge(_cfg(tmp_path), provider=provider)
    result = judge.evaluate({"ticker": "BTC", "strategy": "intraday", "tier": "major"})
    assert result.approved and result.status == "bypassed"
    assert provider.calls == 0
    judge.close()


def test_context_is_bounded_and_contains_no_ambient_state():
    posts = [
        {
            "source": "x",
            "author": "@trader",
            "text": "x" * 1_000,
            "created_at": "2026-08-09T00:00:00Z",
        }
        for _ in range(20)
    ]
    context = safe_judge_context(
        ticker="SOL",
        strategy="swing",
        tier="large",
        setup_id="SOL:swing:2026-08-09T00:00:00Z",
        setup={"setup": "breakout"},
        social_posts=posts,
        max_posts=3,
        max_chars=40,
    )
    assert len(context["recent_social_posts"]) == 3
    assert all(len(p["text"]) == 40 for p in context["recent_social_posts"])
    assert "api_key" not in json.dumps(context).lower()


def test_weekly_reflection_is_advisory_and_persisted(tmp_path):
    cfg = _cfg(tmp_path)
    provider = FakeProvider(
        {
            "summary": "Breakouts worked; stale entries paid too many fees.",
            "strengths": ["SOL breakout entries"],
            "weaknesses": ["late micro entries"],
            "recommendations": ["require stronger volume"],
            "rule_experiments": ["paper-test volume ratio 2.5 for micros"],
        }
    )
    reflector = WeeklyReflector(cfg, provider=provider)
    assert reflector.request("2026-08-09", {"trades": []})
    reflection = None
    for _ in range(50):
        reflection = reflector.poll()
        if reflection is not None:
            break
        time.sleep(0.01)
    assert reflection is not None
    subject, body = reflection.format_alert()
    assert "2026-08-09" in subject
    assert "no trading rule was changed automatically" in body
    saved = json.loads((tmp_path / "reflections.jsonl").read_text(encoding="utf-8"))
    assert saved["rule_experiments"]
    reflector.close()


def test_weekly_reflections_queue_multiple_weeks_durably(tmp_path):
    cfg = _cfg(tmp_path)
    provider = FakeProvider(
        {
            "summary": "summary",
            "strengths": [],
            "weaknesses": [],
            "recommendations": [],
            "rule_experiments": [],
        }
    )
    reflector = WeeklyReflector(cfg, provider=provider)
    assert reflector.request("2026-08-09", {"trades": [1]})
    assert reflector.request("2026-08-16", {"trades": [2]})

    completed = []
    for _ in range(100):
        reflection = reflector.poll()
        if reflection is not None:
            completed.append(reflection.week_ending)
        if len(completed) == 2:
            break
        time.sleep(0.01)
    assert completed == ["2026-08-09", "2026-08-16"]
    assert provider.calls == 2
    assert not reflector.queue_path.exists()
    reflector.close()

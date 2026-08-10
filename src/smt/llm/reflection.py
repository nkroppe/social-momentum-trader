"""Weekly LLM reflection: advisory rule experiments, never auto-applied."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..models import Trade
from .config import LLMConfig
from .provider import CursorJSONProvider

log = get_logger("smt.llm.reflection")


@dataclass(frozen=True)
class WeeklyReflection:
    week_ending: str
    summary: str
    strengths: list[str]
    weaknesses: list[str]
    recommendations: list[str]
    rule_experiments: list[str]
    model: str = ""
    created_at: str = ""

    def format_alert(self) -> tuple[str, str]:
        lines = [self.summary]
        for heading, rows in (
            ("What worked", self.strengths),
            ("What reduced profit", self.weaknesses),
            ("Recommendations", self.recommendations),
            ("Paper-only experiments", self.rule_experiments),
        ):
            if rows:
                lines += ["", f"{heading}:"]
                lines += [f"- {row}" for row in rows]
        lines += [
            "",
            "Advisory only: no trading rule was changed automatically.",
        ]
        return f"LLM weekly reflection — {self.week_ending}", "\n".join(lines)


class WeeklyReflector:
    """Run reflection off the trading loop and persist an audit trail."""

    def __init__(self, cfg: LLMConfig, provider: CursorJSONProvider | None = None):
        self.cfg = cfg
        self.provider = provider or CursorJSONProvider(cfg)
        self.path = Path(cfg.reflection.state_file)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.pending_path = self.path.with_suffix(f"{self.path.suffix}.pending")
        self.queue_path = self.path.with_suffix(f"{self.path.suffix}.queue")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="smt-llm-reflect")
        self._future: Future[WeeklyReflection] | None = None
        self._requested_week: str | None = None
        self._lock = threading.Lock()
        self._retry_after = 0.0
        self._resume_pending()

    def _load_queue(self) -> list[dict[str, Any]]:
        if not self.queue_path.exists():
            return []
        try:
            value = json.loads(self.queue_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return value if isinstance(value, list) else []

    def _save_queue(self, items: list[dict[str, Any]]) -> None:
        if not items:
            self.queue_path.unlink(missing_ok=True)
            return
        temp = self.queue_path.with_suffix(f"{self.queue_path.suffix}.tmp")
        temp.write_text(json.dumps(items), encoding="utf-8")
        temp.replace(self.queue_path)

    def _start(self, week: str, payload: dict[str, Any]) -> None:
        self._requested_week = week
        self.pending_path.write_text(
            json.dumps({"week_ending": week, "payload": payload}),
            encoding="utf-8",
        )
        self._future = self._executor.submit(self._run, week, payload)
        log.info("Queued weekly LLM reflection for %s", week)

    def _start_next(self) -> None:
        if self._future is not None or self.pending_path.exists():
            return
        items = self._load_queue()
        while items:
            item = items.pop(0)
            week = str(item.get("week_ending", ""))
            if week and not self.has_week(week):
                self._save_queue(items)
                self._start(week, item.get("payload", {}))
                return
        self._save_queue([])

    def _resume_pending(self) -> None:
        if not self.pending_path.exists():
            return
        try:
            pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
            week = str(pending["week_ending"])
            payload = pending["payload"]
        except (json.JSONDecodeError, KeyError, OSError, TypeError):
            return
        if self.has_week(week):
            self.pending_path.unlink(missing_ok=True)
            self._requested_week = None
            self._start_next()
            return
        self._requested_week = week
        self._future = self._executor.submit(self._run, week, payload)

    def has_week(self, week_ending: str) -> bool:
        if not self.path.exists():
            return False
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                if json.loads(line).get("week_ending") == week_ending:
                    return True
        except (json.JSONDecodeError, OSError):
            return False
        return False

    def request(self, week_ending: str, payload: dict[str, Any]) -> bool:
        """Durably queue a reflection even when another week is running."""
        if not self.cfg.enabled or not self.cfg.reflection.enabled:
            return False
        with self._lock:
            if self.has_week(week_ending) or self._requested_week == week_ending:
                return True
            items = self._load_queue()
            if any(str(item.get("week_ending")) == week_ending for item in items):
                return True
            if self._future is not None or self.pending_path.exists():
                items.append({"week_ending": week_ending, "payload": payload})
                self._save_queue(items)
                log.info("Persisted queued weekly LLM reflection for %s", week_ending)
                return True
            self._start(week_ending, payload)
            return True

    def poll(self) -> WeeklyReflection | None:
        with self._lock:
            if self._future is None:
                if self.pending_path.exists() and time.monotonic() >= self._retry_after:
                    self._resume_pending()
                return None
            if not self._future.done():
                return None
            future = self._future
            self._future = None
        try:
            reflection = future.result()
        except Exception as exc:  # noqa: BLE001
            log.warning("Weekly LLM reflection failed: %s", exc)
            self._retry_after = time.monotonic() + 3600
            return None
        self._requested_week = None
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(reflection), ensure_ascii=True) + "\n")
        self.pending_path.unlink(missing_ok=True)
        self._start_next()
        return reflection

    def _run(self, week_ending: str, payload: dict[str, Any]) -> WeeklyReflection:
        instruction = """
You are a quantitative trading-review analyst. Review the supplied completed
paper trades, setup metadata, exits, and aggregate performance. Identify
specific ways the deterministic rules may improve risk-adjusted profit.

Constraints:
- Treat the sample as noisy; do not claim statistical significance.
- Separate execution/fee problems from entry and exit problems.
- Recommend only testable paper-trading experiments.
- Never instruct the system to change configuration automatically.
- Do not recommend leverage, derivatives, or bypassing risk limits.

Schema:
{"summary":string <=300 chars,"strengths":[string],"weaknesses":[string],
"recommendations":[string],"rule_experiments":[string]}
"""
        raw = self.provider.complete_json(instruction, payload)

        def rows(name: str, limit: int = 6) -> list[str]:
            value = raw.get(name, [])
            if not isinstance(value, list):
                return []
            return [str(item)[:300] for item in value[:limit]]

        return WeeklyReflection(
            week_ending=week_ending,
            summary=str(raw.get("summary", "No summary returned."))[:300],
            strengths=rows("strengths"),
            weaknesses=rows("weaknesses"),
            recommendations=rows("recommendations"),
            rule_experiments=rows("rule_experiments"),
            model=getattr(self.provider, "_model_id", "") or "",
            created_at=datetime.now(UTC).isoformat(),
        )

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def build_reflection_payload(
    *,
    trades: list[Trade],
    report_subject: str,
    report_body: str,
    rule_snapshot: dict[str, Any],
    max_trades: int,
) -> dict[str, Any]:
    """Serialize bounded, non-secret performance evidence for Sonnet."""
    rows: list[dict[str, Any]] = []
    for trade in trades[:max_trades]:
        risk = max(trade.initial_risk_per_unit * trade.original_qty, 0.0)
        rows.append(
            {
                "ticker": trade.ticker,
                "strategy": trade.strategy,
                "setup": trade.setup,
                "entry_notional": round(trade.entry_notional, 2),
                "realized_pnl": round(trade.realized_pnl, 2),
                "r_multiple": round(trade.realized_pnl / risk, 3) if risk > 0 else None,
                "fees": round(trade.fees_paid, 2),
                "partial_taken": trade.partial_taken,
                "exit_reason": trade.exit_reason.value if trade.exit_reason else "",
                "opened_at": trade.opened_at.isoformat(),
                "closed_at": trade.closed_at.isoformat() if trade.closed_at else None,
            }
        )
    return {
        "weekly_report": {"subject": report_subject, "body": report_body[:8_000]},
        "completed_trades": rows,
        "rule_snapshot": rule_snapshot,
        "instructional_context": (
            "Propose paper-test experiments only. Do not modify rules or place trades."
        ),
    }

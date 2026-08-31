"""Orchestration glue for sparse setup review and weekly reflection."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import MarketConfig, SignalsConfig, StrategyConfig
from ..logging_setup import get_logger
from ..ops.alerts import Alerter
from ..store import Store
from ..trader.signals import TradeCandidate
from .config import LLMConfig
from .judge import SparseL3Judge, safe_judge_context
from .provider import CursorJSONProvider
from .reflection import WeeklyReflection, WeeklyReflector, build_reflection_payload

log = get_logger("smt.llm")


class LLMCoordinator:
    """Keep LLM output advisory and subordinate to deterministic code."""

    def __init__(
        self,
        cfg: LLMConfig,
        store: Store,
        alerter: Alerter,
        strategies: list[StrategyConfig],
        signals: SignalsConfig,
        market: MarketConfig,
        *,
        offline: bool = False,
        extra_email_to: list[str] | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.alerter = alerter
        self.strategies = strategies
        self.signals = signals
        self.market = market
        self.extra_email_to = list(extra_email_to or [])
        self.enabled = bool(cfg.enabled and not offline)
        self.provider = CursorJSONProvider(cfg)
        self.judge = SparseL3Judge(cfg, self.provider)
        self.reflector = WeeklyReflector(cfg, self.provider)
        self.unsent_reflection = Path(f"{cfg.reflection.state_file}.unsent")
        self._audit_key_by_judge_key: dict[str, str] = {}
        self._latest_judge_key_by_audit: dict[str, str] = {}

    def review_candidate(self, candidate: TradeCandidate) -> bool:
        """Apply only L3 catalyst/veto; never create a candidate."""
        if not self.enabled or candidate.tier not in self.cfg.judge.tiers:
            candidate.llm_status = "bypassed"
            candidate.llm_reason = "LLM disabled or tier not configured"
            return True

        posts = self.store.recent_social_events(
            candidate.ticker,
            limit=self.cfg.judge.max_social_posts,
        )
        metadata = dict(candidate.setup_metadata)
        setup_id = str(
            metadata.get("trigger_ts")
            or (
                f"{candidate.ticker}:{candidate.strategy}:{candidate.setup}:"
                f"{float(metadata.get('breakout_level', 0.0)):.8f}"
            )
        )
        context = safe_judge_context(
            ticker=candidate.ticker,
            strategy=candidate.strategy,
            tier=candidate.tier,
            setup_id=setup_id,
            setup={
                "name": candidate.setup,
                "entry_price": round(candidate.entry_price, 8),
                "stop_pct": round(candidate.stop_pct, 6),
                **metadata,
            },
            social_posts=[
                {
                    "source": event.source,
                    "author": event.author,
                    "followers": event.author_followers,
                    "verified": event.author_verified,
                    "sentiment": event.sentiment,
                    "engagement": (
                        event.likes
                        + event.reposts
                        + event.replies
                        + event.quotes
                        + event.bookmarks
                    ),
                    "text": event.text,
                    "created_at": event.created_at.isoformat(),
                }
                for event in posts
            ],
            max_posts=self.cfg.judge.max_social_posts,
            max_chars=self.cfg.judge.max_post_chars,
        )
        decision = self.judge.evaluate(context)
        if decision.key:
            audit_by_judge = getattr(self, "_audit_key_by_judge_key", None)
            latest_by_audit = getattr(self, "_latest_judge_key_by_audit", None)
            if audit_by_judge is None:
                self._audit_key_by_judge_key = {}
                audit_by_judge = self._audit_key_by_judge_key
            if latest_by_audit is None:
                self._latest_judge_key_by_audit = {}
                latest_by_audit = self._latest_judge_key_by_audit
            audit_by_judge[decision.key] = candidate.decision_key
            latest_by_audit[candidate.decision_key] = decision.key
        candidate.llm_status = decision.status
        candidate.llm_score = decision.catalyst_score
        candidate.llm_veto = decision.veto
        candidate.llm_reason = decision.reason
        if self.signals.social_decision_mode == "shadow":
            outcome = (
                "pending"
                if decision.pending
                else "would_pass"
                if decision.approved
                else "would_reject"
            )
            log.info(
                "SHADOW L3[%s] %s %s: status=%s veto=%s score=%.2f (%s); size unchanged",
                candidate.strategy,
                candidate.ticker,
                outcome,
                decision.status,
                decision.veto,
                decision.catalyst_score,
                decision.reason,
            )
            return True
        if decision.pending:
            log.info(
                "L3[%s] %s waiting for Sonnet catalyst/event review",
                candidate.strategy,
                candidate.ticker,
            )
            return False
        if not decision.approved:
            log.info(
                "L3[%s] %s rejected: veto=%s score=%.2f confidence=%.2f (%s)",
                candidate.strategy,
                candidate.ticker,
                decision.veto,
                decision.catalyst_score,
                decision.confidence,
                decision.reason,
            )
            return False

        if decision.catalyst_score > 0.5:
            # LLM may recover some deterministic conviction discount, but the
            # risk gate still hard-caps size at max_position_pct.
            boost = 1.0 + min((decision.catalyst_score - 0.5) * 0.20, 0.10)
            candidate.size_multiplier = min(candidate.size_multiplier * boost, 1.0)
        candidate.reason += (
            f"; L3 score={decision.catalyst_score:.2f} "
            f"confidence={decision.confidence:.2f} {decision.narrative}"
        )
        return True

    def poll_judgements(self) -> None:
        """Persist completed shadow reviews even when their setup disappeared."""
        if not self.enabled or self.signals.social_decision_mode != "shadow":
            return
        for decision in self.judge.poll_completed():
            audit_key = self._audit_key_by_judge_key.pop(decision.key, "")
            if not audit_key:
                continue
            if self._latest_judge_key_by_audit.get(audit_key) != decision.key:
                log.info("SHADOW L3 ignored stale result %s", decision.key)
                continue
            if self.store.update_shadow_llm(
                audit_key,
                status=decision.status,
                score=decision.catalyst_score,
                veto=decision.veto,
                reason=decision.reason,
            ):
                log.info(
                    "SHADOW L3 audit completed key=%s status=%s veto=%s score=%.2f",
                    audit_key,
                    decision.status,
                    decision.veto,
                    decision.catalyst_score,
                )

    def request_weekly_reflection(
        self,
        *,
        occurrence: datetime,
        start: datetime,
        end: datetime,
        report_subject: str,
        report_body: str,
    ) -> bool:
        if not self.enabled or not self.cfg.reflection.enabled:
            return False
        week = occurrence.isoformat()
        trades = list(self.store.closed_trades_between(start, end))
        rules: dict[str, Any] = {
            "strategies": {
                strategy.name: strategy.model_dump(mode="json") for strategy in self.strategies
            },
            "tiers": {
                name: tier.model_dump(mode="json") for name, tier in self.signals.tiers.items()
            },
            "market": self.market.model_dump(mode="json"),
        }
        payload = build_reflection_payload(
            trades=trades,
            report_subject=report_subject,
            report_body=report_body,
            rule_snapshot=rules,
            max_trades=self.cfg.reflection.max_trades,
        )
        return self.reflector.request(week, payload)

    def poll_reflection(self) -> None:
        reflection = self.reflector.poll()
        if reflection is None and self.unsent_reflection.exists():
            try:
                reflection = WeeklyReflection(
                    **json.loads(self.unsent_reflection.read_text(encoding="utf-8"))
                )
            except (json.JSONDecodeError, OSError, TypeError):
                reflection = None
        if reflection is None:
            return
        subject, body = reflection.format_alert()
        if self.cfg.reflection.deliver_telegram and not self.alerter.notify(
            subject, body, critical=False
        ):
            self.unsent_reflection.write_text(
                json.dumps(asdict(reflection)),
                encoding="utf-8",
            )
            log.warning("LLM weekly reflection delivery failed; will retry")
            return
        if self.extra_email_to and not self.alerter.notify_emails(
            subject, body, self.extra_email_to
        ):
            log.warning("LLM weekly reflection extra email failed; Telegram copy already sent")
        self.unsent_reflection.unlink(missing_ok=True)
        log.info("Persisted LLM weekly reflection for %s", reflection.week_ending)

    def reflection_exists(self, occurrence: datetime) -> bool:
        return self.reflector.has_week(occurrence.isoformat())

    def close(self) -> None:
        self.judge.close()
        self.reflector.close()

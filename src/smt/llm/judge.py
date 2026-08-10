"""Non-blocking sparse L3 catalyst judge and adverse-event veto."""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from .config import LLMConfig
from .provider import CursorJSONProvider

log = get_logger("smt.llm.judge")


@dataclass(frozen=True)
class JudgeDecision:
    status: str
    approved: bool
    veto: bool
    catalyst_score: float
    confidence: float
    narrative: str
    reason: str
    key: str
    model: str = ""

    @property
    def pending(self) -> bool:
        return self.status == "pending"


class SparseL3Judge:
    """Queue a model call only after deterministic setup gates have passed.

    The first evaluation of a setup returns ``pending`` without blocking the
    trading loop. The completed result is cached; the next loop can approve or
    veto the same setup. This keeps exit management responsive while Sonnet is
    reasoning.
    """

    def __init__(self, cfg: LLMConfig, provider: CursorJSONProvider | None = None):
        self.cfg = cfg
        self.provider = provider or CursorJSONProvider(cfg)
        self.path = Path(cfg.judge.state_file)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="smt-llm-judge")
        self._pending: dict[str, Future[JudgeDecision]] = {}
        self._latest_key_by_setup: dict[str, str] = {}
        self._setup_key_by_key: dict[str, str] = {}
        self._lock = threading.Lock()
        self._cache = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self) -> None:
        temp = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temp.write_text(json.dumps(self._cache, indent=2), encoding="utf-8")
        temp.replace(self.path)

    @staticmethod
    def _key(context: dict[str, Any]) -> str:
        setup_id = context.get("setup_id")
        if setup_id:
            social = json.dumps(
                context.get("recent_social_posts", []),
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            stable = "|".join(
                (
                    str(context.get("ticker", "")),
                    str(context.get("strategy", "")),
                    str(context.get("tier", "")),
                    str(setup_id),
                    hashlib.sha256(social.encode("utf-8")).hexdigest()[:16],
                )
            )
            return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]
        stable = json.dumps(context, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _setup_key(context: dict[str, Any]) -> str:
        stable = "|".join(
            (
                str(context.get("ticker", "")),
                str(context.get("strategy", "")),
                str(context.get("tier", "")),
                str(context.get("setup_id", "")),
            )
        )
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]

    def _cached(self, key: str) -> JudgeDecision | None:
        item = self._cache.get(key)
        if not item:
            return None
        try:
            expires = datetime.fromisoformat(item["expires_at"])
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if datetime.now(UTC) >= expires:
                del self._cache[key]
                return None
            return JudgeDecision(**item["decision"])
        except (KeyError, TypeError, ValueError):
            self._cache.pop(key, None)
            return None

    def _harvest(self, key: str) -> JudgeDecision | None:
        future = self._pending.get(key)
        if future is None or not future.done():
            return None
        del self._pending[key]
        try:
            decision = future.result()
        except Exception as exc:  # noqa: BLE001
            log.warning("L3 judge failed for %s: %s", key, exc)
            decision = JudgeDecision(
                status="error",
                approved=False,
                veto=True,
                catalyst_score=0.0,
                confidence=0.0,
                narrative="unavailable",
                reason=str(exc),
                key=key,
            )
        expires = datetime.now(UTC) + timedelta(minutes=self.cfg.judge.cache_ttl_minutes)
        self._cache[key] = {
            "expires_at": expires.isoformat(),
            "decision": asdict(decision),
        }
        self._save()
        return decision

    def evaluate(self, context: dict[str, Any]) -> JudgeDecision:
        """Return cached decision, queue a new call, or report pending."""
        tier = str(context.get("tier", ""))
        if not self.cfg.enabled or not self.cfg.judge.enabled or tier not in self.cfg.judge.tiers:
            return JudgeDecision(
                status="bypassed",
                approved=True,
                veto=False,
                catalyst_score=0.5,
                confidence=1.0,
                narrative="not applicable",
                reason=f"LLM judge bypassed for tier {tier}",
                key="",
            )

        key = self._key(context)
        setup_key = self._setup_key(context)
        with self._lock:
            prior_key = self._latest_key_by_setup.get(setup_key)
            if prior_key and prior_key != key:
                prior = self._pending.get(prior_key)
                if prior is not None and prior.done():
                    self._harvest(prior_key)
                    self._setup_key_by_key.pop(prior_key, None)
                elif prior is not None and prior.cancel():
                    self._pending.pop(prior_key, None)
                    self._setup_key_by_key.pop(prior_key, None)
            self._latest_key_by_setup[setup_key] = key
            self._setup_key_by_key[key] = setup_key
            harvested = self._harvest(key)
            if harvested is not None:
                return harvested
            cached = self._cached(key)
            if cached is not None:
                return cached
            if key not in self._pending:
                self._pending[key] = self._executor.submit(self._run, key, context)
                log.info(
                    "Queued sparse L3 judge for %s [%s/%s]",
                    context.get("ticker"),
                    context.get("strategy"),
                    tier,
                )

        return JudgeDecision(
            status="pending",
            approved=False,
            veto=False,
            catalyst_score=0.0,
            confidence=0.0,
            narrative="pending",
            reason="LLM catalyst/event review pending",
            key=key,
        )

    def poll_completed(self) -> list[JudgeDecision]:
        """Harvest finished jobs without requiring the price setup to recur."""
        completed: list[JudgeDecision] = []
        with self._lock:
            for key in list(self._pending):
                decision = self._harvest(key)
                if decision is None:
                    continue
                setup_key = self._setup_key_by_key.pop(key, "")
                if self._latest_key_by_setup.get(setup_key) == key:
                    completed.append(decision)
                else:
                    log.info("Discarded stale L3 result %s for superseded context", key)
        return completed

    def _run(self, key: str, context: dict[str, Any]) -> JudgeDecision:
        instruction = """
You are a conservative crypto catalyst and adverse-event reviewer. A
deterministic price-action system has already found a technically valid long
setup. Judge only the supplied social/news context.

Tasks:
1. Set veto=true for credible adverse events (hack/exploit, delisting,
regulatory action, insolvency, rug/scam evidence, severe operational failure)
or obvious coordinated pump/spam.
2. Score whether the context is a genuine positive catalyst that can sustain
1-3 day momentum. Do not infer facts absent from the input.
3. Never recommend order size, stop, target, leverage, or an actual trade.

Schema:
{"veto":bool,"catalyst_score":number 0..1,"confidence":number 0..1,
"narrative":string <=80 chars,"reason":string <=240 chars}
"""
        raw = self.provider.complete_json(instruction, context)
        veto = bool(raw.get("veto", True))
        score = max(0.0, min(float(raw.get("catalyst_score", 0.0)), 1.0))
        confidence = max(0.0, min(float(raw.get("confidence", 0.0)), 1.0))
        required = str(context.get("tier")) in self.cfg.judge.required_tiers
        approved = not veto and (not required or score >= self.cfg.judge.min_catalyst_score)
        return JudgeDecision(
            status="complete",
            approved=approved,
            veto=veto,
            catalyst_score=score,
            confidence=confidence,
            narrative=str(raw.get("narrative", ""))[:80],
            reason=str(raw.get("reason", ""))[:240],
            key=key,
            model=getattr(self.provider, "_model_id", "") or "",
        )

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def safe_judge_context(
    *,
    ticker: str,
    strategy: str,
    tier: str,
    setup_id: str,
    setup: dict[str, Any],
    social_posts: list[dict[str, Any]],
    max_posts: int,
    max_chars: int,
) -> dict[str, Any]:
    """Build a secret-free, bounded payload for the text-only agent."""
    posts = []
    for post in social_posts[:max_posts]:
        posts.append(
            {
                "source": str(post.get("source", ""))[:16],
                "author": str(post.get("author", ""))[:80],
                "followers": max(int(post.get("followers", 0) or 0), 0),
                "verified": bool(post.get("verified", False)),
                "sentiment": max(
                    -1.0, min(float(post.get("sentiment", 0.0) or 0.0), 1.0)
                ),
                "engagement": max(int(post.get("engagement", 0) or 0), 0),
                "text": str(post.get("text", ""))[:max_chars],
                "created_at": str(post.get("created_at", "")),
            }
        )
    return {
        "ticker": ticker,
        "strategy": strategy,
        "tier": tier,
        "setup_id": setup_id,
        "technical_setup": setup,
        "recent_social_posts": posts,
    }

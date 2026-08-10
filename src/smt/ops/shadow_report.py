"""Read-only evidence report for staged social and Sonnet activation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean

from ..config import (
    ShadowReportConfig,
    SignalsConfig,
    SourcesConfig,
    UniverseConfig,
)
from ..ingest.base import extract_tickers
from ..llm.config import LLMConfig
from ..models import ShadowDecision, Trade, TradeStatus
from ..store import CountCoverage, Store

RISK_EPSILON = 1e-9


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class OutcomeStats:
    decisions: int
    linked_closed: int
    wins: int
    total_pnl: float
    mean_pnl: float
    average_r: float | None
    r_unavailable: int
    profitable_rejects: int = 0
    profitable_reject_pnl: float = 0.0


@dataclass(frozen=True)
class TierReadiness:
    tier: str
    ready: bool
    reasons: tuple[str, ...]
    passed: OutcomeStats
    rejected: OutcomeStats
    expectancy_gap_r: float | None


@dataclass(frozen=True)
class ShadowReportSummary:
    start: datetime
    end: datetime
    decision_mode: str
    observation_days: int
    coverage: dict[str, CountCoverage]
    social_ready: bool
    sonnet_ready: bool
    social_reasons: tuple[str, ...]
    sonnet_reasons: tuple[str, ...]
    social_overall_passed: OutcomeStats
    social_overall_rejected: OutcomeStats
    sonnet_overall_passed: OutcomeStats
    sonnet_overall_rejected: OutcomeStats
    social_tiers: dict[str, TierReadiness]
    sonnet_tiers: dict[str, TierReadiness]


def _closed_trade(
    decision: ShadowDecision, trades: dict[int, Trade], end: datetime
) -> Trade | None:
    trade = trades.get(int(decision.trade_id or 0))
    if (
        trade is None
        or trade.status != TradeStatus.CLOSED
        or trade.closed_at is None
        or _aware(trade.closed_at) > end
    ):
        return None
    return trade


def _outcome_stats(
    decisions: list[ShadowDecision],
    trades: dict[int, Trade],
    end: datetime,
    *,
    rejected: bool = False,
) -> OutcomeStats:
    closed = [trade for decision in decisions if (trade := _closed_trade(decision, trades, end))]
    rs: list[float] = []
    unavailable = 0
    for trade in closed:
        basis = (trade.initial_risk_per_unit or 0.0) * (trade.original_qty or 0.0)
        if basis <= RISK_EPSILON:
            unavailable += 1
        else:
            rs.append(trade.realized_pnl / max(basis, RISK_EPSILON))
    profitable = [trade for trade in closed if trade.realized_pnl > 0] if rejected else []
    total = sum(trade.realized_pnl for trade in closed)
    return OutcomeStats(
        decisions=len(decisions),
        linked_closed=len(closed),
        wins=sum(trade.realized_pnl > 0 for trade in closed),
        total_pnl=total,
        mean_pnl=total / len(closed) if closed else 0.0,
        average_r=mean(rs) if rs else None,
        r_unavailable=unavailable,
        profitable_rejects=len(profitable),
        profitable_reject_pnl=sum(trade.realized_pnl for trade in profitable),
    )


def _common_reasons(
    cfg: ShadowReportConfig,
    signals: SignalsConfig,
    observation_days: int,
    coverage: dict[str, CountCoverage],
    tier_tickers: list[str],
) -> list[str]:
    reasons: list[str] = []
    if signals.social_decision_mode != "shadow":
        reasons.append("decision mode is not shadow")
    if observation_days < cfg.min_observation_days:
        reasons.append(
            f"only {observation_days} observation days (need {cfg.min_observation_days})"
        )
    if not tier_tickers:
        reasons.append("no configured X count ticker for tier")
    for ticker in tier_tickers:
        item = coverage.get(ticker)
        ratio = item.ratio if item else 0.0
        if ratio < cfg.min_count_coverage:
            reasons.append(
                f"{ticker} count coverage {ratio:.1%} < {cfg.min_count_coverage:.1%}"
            )
    return reasons


def _performance_reasons(
    cfg: ShadowReportConfig,
    passed: OutcomeStats,
    rejected: OutcomeStats,
) -> tuple[list[str], float | None]:
    reasons: list[str] = []
    closed = passed.linked_closed + rejected.linked_closed
    if closed < cfg.min_closed_linked_trades_per_tier:
        reasons.append(
            f"{closed} linked closed trades < {cfg.min_closed_linked_trades_per_tier}"
        )
    floor = cfg.min_completed_per_outcome_group
    if passed.linked_closed < floor:
        reasons.append(
            f"pass group has {passed.linked_closed} linked closed outcomes < {floor}"
        )
    if rejected.linked_closed < floor:
        reasons.append(
            f"reject group has {rejected.linked_closed} linked closed outcomes < {floor}"
        )
    gap = (
        passed.average_r - rejected.average_r
        if passed.average_r is not None and rejected.average_r is not None
        else None
    )
    if gap is None:
        reasons.append("pass-vs-reject net R separation unavailable")
    elif gap < cfg.min_expectancy_separation_r:
        reasons.append(
            f"expectancy separation {gap:+.2f}R < {cfg.min_expectancy_separation_r:.2f}R"
        )
    return reasons, gap


def _tier_tickers(universe: UniverseConfig, tier: str, count_tickers: set[str]) -> list[str]:
    return sorted(
        ticker
        for ticker in count_tickers
        if ticker in universe.symbols and universe.tier_of(ticker) == tier
    )


def analyze_shadow_readiness(
    store: Store,
    cfg: ShadowReportConfig,
    sources: SourcesConfig,
    signals: SignalsConfig,
    universe: UniverseConfig,
    llm: LLMConfig,
    start: datetime,
    end: datetime,
) -> ShadowReportSummary:
    """Compute deterministic, fail-closed readiness evidence for one UTC window."""
    decisions = list(store.shadow_decisions_between(start, end))
    trades = store.trades_by_ids(decision.trade_id or 0 for decision in decisions)
    count_tickers = {
        ticker
        for keyword in sources.x.keywords
        for ticker in extract_tickers(keyword, universe)
    }
    coverage = {
        ticker: store.count_coverage(
            ticker, start, end, sources.x.count_window_minutes, source="x"
        )
        for ticker in sorted(count_tickers)
    }
    observation_days = store.count_observation_days(count_tickers, start, end)
    universe_tiers = {
        universe.tier_of(ticker, signals.default_tier) for ticker in universe.symbols
    }
    actionable_social = sorted(
        tier
        for tier in universe_tiers
        if signals.tier(tier).social_policy != "ignored"
    )

    social_tiers: dict[str, TierReadiness] = {}
    for tier in actionable_social:
        rows = [decision for decision in decisions if decision.tier == tier]
        passed_rows = [
            decision
            for decision in rows
            if decision.social_decision in {"would_pass", "would_boost"}
        ]
        rejected_rows = [
            decision for decision in rows if decision.social_decision == "would_reject"
        ]
        passed = _outcome_stats(passed_rows, trades, end)
        rejected = _outcome_stats(rejected_rows, trades, end, rejected=True)
        reasons = _common_reasons(
            cfg,
            signals,
            observation_days,
            coverage,
            _tier_tickers(universe, tier, count_tickers),
        )
        if not sources.x.enabled:
            reasons.append("X source is disabled in current config")
        if not sources.x.counts_enabled:
            reasons.append("X recent counts are disabled in current config")
        performance, gap = _performance_reasons(cfg, passed, rejected)
        reasons.extend(performance)
        social_tiers[tier] = TierReadiness(
            tier, not reasons, tuple(reasons), passed, rejected, gap
        )

    sonnet_tiers: dict[str, TierReadiness] = {}
    for tier in sorted(set(llm.judge.tiers) & universe_tiers):
        rows = [decision for decision in decisions if decision.tier == tier]
        completed = [decision for decision in rows if decision.llm_status == "complete"]
        passed_rows: list[ShadowDecision] = []
        rejected_rows: list[ShadowDecision] = []
        for decision in completed:
            would_reject = decision.llm_veto or (
                tier in llm.judge.required_tiers
                and decision.llm_score < llm.judge.min_catalyst_score
            )
            (rejected_rows if would_reject else passed_rows).append(decision)
        passed = _outcome_stats(passed_rows, trades, end)
        rejected = _outcome_stats(rejected_rows, trades, end, rejected=True)
        reasons = _common_reasons(
            cfg,
            signals,
            observation_days,
            coverage,
            _tier_tickers(universe, tier, count_tickers),
        )
        if not llm.enabled:
            reasons.append("LLM is disabled in current config")
        if not llm.judge.enabled:
            reasons.append("Sonnet judge is disabled in current config")
        eligible = sum(
            decision.llm_status in {"complete", "error", "pending"}
            for decision in rows
        )
        completion_rate = len(completed) / eligible if eligible else 0.0
        if completion_rate < cfg.min_llm_completion_rate:
            reasons.append(
                f"LLM completion rate {completion_rate:.1%} "
                f"< {cfg.min_llm_completion_rate:.1%}"
            )
        judged = sum(decision.llm_status in {"complete", "error"} for decision in rows)
        errors = sum(decision.llm_status == "error" for decision in rows)
        error_rate = errors / judged if judged else 0.0
        if error_rate > cfg.max_llm_error_rate:
            reasons.append(
                f"LLM error rate {error_rate:.1%} > {cfg.max_llm_error_rate:.1%}"
            )
        performance, gap = _performance_reasons(cfg, passed, rejected)
        reasons.extend(performance)
        sonnet_tiers[tier] = TierReadiness(
            tier, not reasons, tuple(reasons), passed, rejected, gap
        )

    social_reasons = tuple(
        f"{tier}: {reason}"
        for tier, item in social_tiers.items()
        for reason in item.reasons
    )
    sonnet_reasons = tuple(
        f"{tier}: {reason}"
        for tier, item in sonnet_tiers.items()
        for reason in item.reasons
    )
    if not social_tiers:
        social_reasons = ("no actionable social tiers configured",)
    if not sonnet_tiers:
        sonnet_reasons = ("no configured Sonnet judge tiers in universe",)
    social_rows = [
        decision for decision in decisions if decision.tier in actionable_social
    ]
    social_passed_rows = [
        decision
        for decision in social_rows
        if decision.social_decision in {"would_pass", "would_boost"}
    ]
    social_rejected_rows = [
        decision
        for decision in social_rows
        if decision.social_decision == "would_reject"
    ]
    sonnet_rows = [
        decision for decision in decisions if decision.tier in sonnet_tiers
    ]
    sonnet_completed = [
        decision for decision in sonnet_rows if decision.llm_status == "complete"
    ]
    sonnet_rejected_rows = [
        decision
        for decision in sonnet_completed
        if decision.llm_veto
        or (
            decision.tier in llm.judge.required_tiers
            and decision.llm_score < llm.judge.min_catalyst_score
        )
    ]
    rejected_ids = {decision.id for decision in sonnet_rejected_rows}
    sonnet_passed_rows = [
        decision for decision in sonnet_completed if decision.id not in rejected_ids
    ]
    return ShadowReportSummary(
        start=start,
        end=end,
        decision_mode=signals.social_decision_mode,
        observation_days=observation_days,
        coverage=coverage,
        social_ready=bool(social_tiers) and not social_reasons,
        sonnet_ready=bool(sonnet_tiers) and not sonnet_reasons,
        social_reasons=social_reasons,
        sonnet_reasons=sonnet_reasons,
        social_overall_passed=_outcome_stats(social_passed_rows, trades, end),
        social_overall_rejected=_outcome_stats(
            social_rejected_rows, trades, end, rejected=True
        ),
        sonnet_overall_passed=_outcome_stats(sonnet_passed_rows, trades, end),
        sonnet_overall_rejected=_outcome_stats(
            sonnet_rejected_rows, trades, end, rejected=True
        ),
        social_tiers=social_tiers,
        sonnet_tiers=sonnet_tiers,
    )


def _r(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}R"


def _group_line(label: str, stats: OutcomeStats) -> str:
    win_rate = stats.wins / stats.linked_closed if stats.linked_closed else 0.0
    return (
        f"    {label}: decisions={stats.decisions} closed={stats.linked_closed} "
        f"win={win_rate:.0%} net=${stats.total_pnl:+.2f} mean=${stats.mean_pnl:+.2f} "
        f"avgR={_r(stats.average_r)} R-missing={stats.r_unavailable}"
    )


def _tier_lines(items: dict[str, TierReadiness]) -> list[str]:
    lines: list[str] = []
    for tier, item in items.items():
        lines.append(f"  {tier}: {'READY' if item.ready else 'NOT READY'}")
        lines.append(_group_line("pass", item.passed))
        lines.append(_group_line("reject", item.rejected))
        lines.append(f"    separation={_r(item.expectancy_gap_r)}")
        if item.rejected.profitable_rejects:
            lines.append(
                f"    false rejects: {item.rejected.profitable_rejects} profitable, "
                f"net=${item.rejected.profitable_reject_pnl:+.2f}"
            )
        for reason in item.reasons:
            lines.append(f"    - {reason}")
    return lines


def _overall_lines(passed: OutcomeStats, rejected: OutcomeStats) -> list[str]:
    gap = (
        passed.average_r - rejected.average_r
        if passed.average_r is not None and rejected.average_r is not None
        else None
    )
    lines = [
        "  overall:",
        _group_line("pass", passed),
        _group_line("reject", rejected),
        f"    separation={_r(gap)}",
    ]
    if rejected.profitable_rejects:
        lines.append(
            f"    false rejects: {rejected.profitable_rejects} profitable, "
            f"net=${rejected.profitable_reject_pnl:+.2f}"
        )
    return lines


def build_shadow_report(
    store: Store,
    cfg: ShadowReportConfig,
    sources: SourcesConfig,
    signals: SignalsConfig,
    universe: UniverseConfig,
    llm: LLMConfig,
    start: datetime,
    end: datetime,
) -> tuple[str, str]:
    """Return a compact plain-text advisory report without external calls."""
    summary = analyze_shadow_readiness(
        store, cfg, sources, signals, universe, llm, start, end
    )
    decisions = list(store.shadow_decisions_between(start, end))
    trades = store.trades_by_ids(decision.trade_id or 0 for decision in decisions)
    linked = [decision for decision in decisions if int(decision.trade_id or 0) in trades]
    closed = [
        decision for decision in linked if _closed_trade(decision, trades, end) is not None
    ]
    open_count = sum(
        trades[int(decision.trade_id or 0)].status == TradeStatus.OPEN for decision in linked
    )
    sonnet_decisions = [
        decision for decision in decisions if decision.tier in llm.judge.tiers
    ]
    statuses = Counter(
        decision.llm_status or "unknown" for decision in sonnet_decisions
    )
    eligible_statuses = sum(
        statuses[status] for status in ("complete", "error", "pending")
    )
    complete_or_error = statuses["complete"] + statuses["error"]
    completion_rate = (
        statuses["complete"] / eligible_statuses if eligible_statuses else 0.0
    )
    error_rate = statuses["error"] / complete_or_error if complete_or_error else 0.0
    latencies = [
        (
            _aware(decision.llm_completed_at) - _aware(decision.first_evaluated_at)
        ).total_seconds()
        for decision in sonnet_decisions
        if decision.llm_completed_at is not None
        and decision.llm_status in {"complete", "error"}
        and _aware(decision.llm_completed_at) >= _aware(decision.first_evaluated_at)
    ]

    overall_expected = sum(item.expected for item in summary.coverage.values())
    overall_observed = sum(item.observed for item in summary.coverage.values())
    overall_coverage = (
        overall_observed / overall_expected if overall_expected else 0.0
    )
    social_tier_lines = _tier_lines(summary.social_tiers) or [
        f"  - {reason}" for reason in summary.social_reasons
    ]
    sonnet_tier_lines = _tier_lines(summary.sonnet_tiers) or [
        f"  - {reason}" for reason in summary.sonnet_reasons
    ]
    subject = (
        f"Shadow readiness: SOCIAL {'READY' if summary.social_ready else 'NOT READY'}; "
        f"L3 {'READY' if summary.sonnet_ready else 'NOT READY'}"
    )
    lines = [
        "SHADOW READINESS REPORT (advisory only)",
        f"UTC window: {_aware(start):%Y-%m-%d %H:%M} to {_aware(end):%Y-%m-%d %H:%M}",
        f"Decision mode: {summary.decision_mode}",
        f"Observed calendar span: {summary.observation_days} distinct UTC count days",
        "",
        f"Count coverage: {overall_observed}/{overall_expected} ({overall_coverage:.1%})",
    ]
    lines.extend(
        f"  {ticker}: {item.observed}/{item.expected} ({item.ratio:.1%})"
        for ticker, item in summary.coverage.items()
    )
    tier_counts = Counter(decision.tier for decision in decisions)
    strategy_counts = Counter(decision.strategy for decision in decisions)
    risk_approved = sum(
        decision.risk_status == "approved" or int(decision.trade_id or 0) in trades
        for decision in decisions
    )
    lines += [
        "",
        "Decision funnel:",
        f"  audited={len(decisions)} risk-approved={risk_approved} "
        f"linked={len(linked)} closed={len(closed)} open={open_count} "
        f"unlinked={len(decisions) - len(linked)}",
        "  tiers: " + (", ".join(f"{k}={v}" for k, v in sorted(tier_counts.items())) or "none"),
        "  strategies: "
        + (", ".join(f"{k}={v}" for k, v in sorted(strategy_counts.items())) or "none"),
        "",
        f"SOCIAL: {'READY' if summary.social_ready else 'NOT READY'}",
        *_overall_lines(
            summary.social_overall_passed, summary.social_overall_rejected
        ),
        *social_tier_lines,
        "",
        f"SONNET L3: {'READY' if summary.sonnet_ready else 'NOT READY'}",
        (
            "  statuses: "
            f"pending={statuses['pending']} complete={statuses['complete']} "
            f"error={statuses['error']} bypassed={statuses['bypassed']} "
            f"completion={completion_rate:.1%} error={error_rate:.1%}"
        ),
        (
            f"  reliable completion latency avg={mean(latencies):.1f}s n={len(latencies)}"
            if latencies
            else "  reliable completion latency: unavailable"
        ),
        *_overall_lines(
            summary.sonnet_overall_passed, summary.sonnet_overall_rejected
        ),
        *sonnet_tier_lines,
        "",
        "Readiness is advisory, does not claim statistical certainty, and never changes config.",
        "Activate only READY tiers in a separately reviewed staged paper rollout.",
    ]
    return subject, "\n".join(lines)

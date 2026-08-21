"""Pure exit planning and state transitions shared by PAPER and replay tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from ..config import ExitProfileConfig
from ..market import Candle
from ..models import ExitReason

ActionKind = Literal["none", "partial", "close"]


@dataclass(frozen=True)
class ExitPlan:
    stop_loss: float
    take_profit: float
    initial_risk_per_unit: float
    profile: ExitProfileConfig
    stale_stop_at: datetime
    time_stop_at: datetime
    note: str


@dataclass(frozen=True)
class ExitState:
    entry_price: float
    qty: float
    original_qty: float
    highest_price: float
    partial_taken: bool
    trailing_stop: float


@dataclass(frozen=True)
class ExitAction:
    kind: ActionKind
    reason: ExitReason = ExitReason.NONE
    reference_price: float = 0.0
    qty: float = 0.0


@dataclass(frozen=True)
class ExitStep:
    action: ExitAction
    highest_price: float
    trailing_stop: float


def compute_exit_levels(
    *,
    entry_price: float,
    structure_stop: float,
    structure_stop_pct: float,
    atr_pct: float,
    horizon_vol_pct: float,
    assumed_fee_pct_per_side: float,
    profile: ExitProfileConfig,
) -> tuple[float, float, str]:
    """Compute deterministic entry levels, preferring the setup's structure stop."""
    if 0 < structure_stop < entry_price:
        stop_loss = round(structure_stop, 8)
        risk_per_unit = entry_price - stop_loss
        take_profit = round(entry_price + risk_per_unit * profile.partial_take_profit_r, 8)
        return (
            take_profit,
            stop_loss,
            f"{profile.label} structure stop={structure_stop_pct:.2%} "
            f"partial={profile.partial_take_profit_r:.2f}R",
        )

    take_profit_pct = profile.take_profit_pct
    stop_loss_pct = profile.stop_loss_pct
    note = "fixed"
    if profile.exit_style == "atr" and atr_pct > 0 and horizon_vol_pct > 0:
        stop_loss_pct = horizon_vol_pct * profile.atr_stop_loss_mult
        stop_loss_pct = max(
            profile.atr_min_stop_pct,
            min(stop_loss_pct, profile.atr_max_stop_pct),
        )
        reward_risk = profile.atr_take_profit_mult / profile.atr_stop_loss_mult
        take_profit_pct = stop_loss_pct * reward_risk
        note = f"atr={atr_pct:.2%}/bar horizon={horizon_vol_pct:.2%}"
    elif profile.exit_style == "atr":
        note = "fixed (no ATR history)"

    fee_floor = 3.0 * assumed_fee_pct_per_side
    if take_profit_pct < fee_floor:
        take_profit_pct = fee_floor
        note += " tp raised to fee floor"
    take_profit = round(entry_price * (1 + take_profit_pct), 8)
    stop_loss = round(entry_price * (1 - stop_loss_pct), 8)
    return (
        take_profit,
        stop_loss,
        f"{profile.label} {note} tp={take_profit_pct:.2%} sl={stop_loss_pct:.2%}",
    )


def build_exit_plan(
    *,
    entry_price: float,
    structure_stop: float,
    structure_stop_pct: float,
    atr_pct: float,
    horizon_vol_pct: float,
    assumed_fee_pct_per_side: float,
    profile: ExitProfileConfig,
    opened_at: datetime,
) -> ExitPlan:
    take_profit, stop_loss, note = compute_exit_levels(
        entry_price=entry_price,
        structure_stop=structure_stop,
        structure_stop_pct=structure_stop_pct,
        atr_pct=atr_pct,
        horizon_vol_pct=horizon_vol_pct,
        assumed_fee_pct_per_side=assumed_fee_pct_per_side,
        profile=profile,
    )
    return ExitPlan(
        stop_loss=stop_loss,
        take_profit=take_profit,
        initial_risk_per_unit=max(entry_price - stop_loss, 0.0),
        profile=profile,
        stale_stop_at=opened_at + timedelta(hours=profile.stale_time_stop_hours),
        time_stop_at=opened_at + timedelta(hours=profile.time_stop_hours),
        note=note,
    )


def active_stop(stop_loss: float, state: ExitState) -> float:
    return max(stop_loss, state.trailing_stop if state.partial_taken else 0.0)


def _mfe_r(state: ExitState, initial_risk_per_unit: float, current_high: float) -> float:
    if initial_risk_per_unit <= 0:
        return 0.0
    return max(current_high - state.entry_price, 0.0) / initial_risk_per_unit


def chandelier_stop(
    *,
    state: ExitState,
    stop_loss: float,
    atr_abs: float,
    multiplier: float,
    current_high: float,
) -> float:
    effective_atr = atr_abs if atr_abs > 0 else max(state.entry_price - stop_loss, 0.0)
    if effective_atr <= 0:
        return state.trailing_stop
    proposed = current_high - multiplier * effective_atr
    return max(state.trailing_stop, stop_loss, proposed)


def first_partial_net_profit(
    *,
    entry_price: float,
    target_price: float,
    notional_usd: float,
    partial_fraction: float,
    fee_pct_per_side: float,
) -> float:
    if entry_price <= 0 or target_price <= entry_price or notional_usd <= 0:
        return 0.0
    partial_qty = (notional_usd / entry_price) * partial_fraction
    gross = (target_price - entry_price) * partial_qty
    entry_fee_share = notional_usd * fee_pct_per_side * partial_fraction
    exit_fee = target_price * partial_qty * fee_pct_per_side
    return gross - entry_fee_share - exit_fee


def step_quote(
    *,
    state: ExitState,
    plan: ExitPlan,
    price: float,
    atr_abs: float,
    now: datetime,
) -> ExitStep:
    """Evaluate one quote. Protective stops always win ambiguous ordering."""
    high = max(state.highest_price, price)
    trail = state.trailing_stop
    stop = active_stop(plan.stop_loss, state)
    if price <= stop:
        reason = (
            ExitReason.TRAILING_STOP
            if state.partial_taken and stop > plan.stop_loss
            else ExitReason.STOP_LOSS
        )
        return ExitStep(ExitAction("close", reason, stop, state.qty), high, trail)

    if not plan.profile.advanced_exit_enabled:
        if price >= plan.take_profit:
            return ExitStep(
                ExitAction("close", ExitReason.TAKE_PROFIT, plan.take_profit, state.qty),
                high,
                trail,
            )
    elif not state.partial_taken and price >= plan.take_profit:
        partial_qty = min(
            state.original_qty * plan.profile.partial_take_profit_fraction,
            state.qty,
        )
        trail = chandelier_stop(
            state=state,
            stop_loss=plan.stop_loss,
            atr_abs=atr_abs,
            multiplier=plan.profile.chandelier_atr_mult,
            current_high=high,
        )
        return ExitStep(
            ExitAction("partial", ExitReason.TAKE_PROFIT, plan.take_profit, partial_qty),
            high,
            trail,
        )
    elif state.partial_taken:
        trail = chandelier_stop(
            state=state,
            stop_loss=plan.stop_loss,
            atr_abs=atr_abs,
            multiplier=plan.profile.chandelier_atr_mult,
            current_high=high,
        )
        if price <= trail:
            return ExitStep(
                ExitAction("close", ExitReason.TRAILING_STOP, trail, state.qty),
                high,
                trail,
            )

    progress = _mfe_r(state, plan.initial_risk_per_unit, high)
    stale = (
        not state.partial_taken
        and now >= plan.stale_stop_at
        and progress < plan.profile.stale_mfe_r
    )
    if stale:
        return ExitStep(
            ExitAction("close", ExitReason.STALE_TIME_STOP, price, state.qty),
            high,
            trail,
        )
    if now >= plan.time_stop_at:
        return ExitStep(
            ExitAction("close", ExitReason.TIME_STOP, price, state.qty),
            high,
            trail,
        )
    return ExitStep(ExitAction("none"), high, trail)


def step_bar(
    *,
    state: ExitState,
    plan: ExitPlan,
    bar: Candle,
    atr_abs: float,
    now: datetime,
) -> ExitStep:
    """Replay one OHLCV bar conservatively using stop-first ordering."""
    high = max(state.highest_price, bar.high)
    stop = active_stop(plan.stop_loss, state)
    if bar.low <= stop:
        reason = (
            ExitReason.TRAILING_STOP
            if state.partial_taken and stop > plan.stop_loss
            else ExitReason.STOP_LOSS
        )
        return ExitStep(
            ExitAction("close", reason, stop, state.qty),
            high,
            state.trailing_stop,
        )
    if not plan.profile.advanced_exit_enabled and bar.high >= plan.take_profit:
        return ExitStep(
            ExitAction("close", ExitReason.TAKE_PROFIT, plan.take_profit, state.qty),
            high,
            state.trailing_stop,
        )
    partial_hit = (
        plan.profile.advanced_exit_enabled
        and not state.partial_taken
        and bar.high >= plan.take_profit
    )
    if partial_hit:
        partial_qty = min(
            state.original_qty * plan.profile.partial_take_profit_fraction,
            state.qty,
        )
        trail = chandelier_stop(
            state=state,
            stop_loss=plan.stop_loss,
            atr_abs=atr_abs,
            multiplier=plan.profile.chandelier_atr_mult,
            current_high=high,
        )
        return ExitStep(
            ExitAction("partial", ExitReason.TAKE_PROFIT, plan.take_profit, partial_qty),
            high,
            trail,
        )
    return step_quote(
        state=ExitState(
            entry_price=state.entry_price,
            qty=state.qty,
            original_qty=state.original_qty,
            highest_price=high,
            partial_taken=state.partial_taken,
            trailing_stop=state.trailing_stop,
        ),
        plan=plan,
        price=bar.close,
        atr_abs=atr_abs,
        now=now,
    )


def legacy_exit_profile(strategy_name: str) -> ExitProfileConfig:
    """Preserve pre-deployment semantics for open trades without snapshots."""
    if strategy_name == "swing":
        return ExitProfileConfig(
            label="legacy_swing_v1",
            partial_take_profit_fraction=0.50,
            partial_take_profit_r=2.0,
            chandelier_atr_mult=3.0,
            trail_granularity_seconds=3_600,
            stale_time_stop_hours=24,
            time_stop_hours=48,
        )
    return ExitProfileConfig(
        label="legacy_intraday_v1",
        partial_take_profit_fraction=0.50,
        partial_take_profit_r=1.5,
        chandelier_atr_mult=3.0,
        trail_granularity_seconds=900,
        stale_time_stop_hours=4,
        time_stop_hours=6,
    )

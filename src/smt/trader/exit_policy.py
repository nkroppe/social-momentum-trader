"""Pure exit-policy primitives shared by PAPER, risk checks, and backtests."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class ExitMode(StrEnum):
    PARTIAL_TRAIL = "partial_trail"
    BOUNDED_TARGET = "bounded_target"


class ExitActionKind(StrEnum):
    PARTIAL = "partial"
    CLOSE = "close"


@dataclass(frozen=True)
class ResolvedExitProfile:
    label: str
    mode: str
    take_profit_pct: float
    stop_loss_pct: float
    time_stop_hours: int
    exit_style: str
    atr_take_profit_mult: float
    atr_stop_loss_mult: float
    atr_min_stop_pct: float
    atr_max_stop_pct: float
    advanced_exit_enabled: bool
    partial_take_profit_fraction: float
    partial_take_profit_r: float
    chandelier_atr_mult: float
    trail_granularity_seconds: int
    stale_time_stop_hours: int
    stale_mfe_r: float

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InitialLevels:
    take_profit: float
    stop_loss: float
    note: str


@dataclass(frozen=True)
class FirstPartialEconomics:
    qty: float
    gross_profit: float
    entry_fee_share: float
    exit_cost: float
    net_profit: float


@dataclass(frozen=True)
class ExitAction:
    kind: ExitActionKind
    reason: str
    price: float
    qty: float = 0.0


@dataclass(frozen=True)
class PolicyStep:
    actions: tuple[ExitAction, ...]
    highest_price: float
    trailing_stop: float


def resolve_profile(value: Any) -> ResolvedExitProfile:
    """Resolve a Pydantic config, mapping snapshot, or already-resolved profile."""
    if isinstance(value, ResolvedExitProfile):
        return value
    if hasattr(value, "model_dump"):
        raw = value.model_dump(mode="python")
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raise TypeError("exit profile must be a config model or mapping")
    return ResolvedExitProfile(**{name: raw[name] for name in ResolvedExitProfile.__annotations__})


def legacy_profile(strategy: str) -> ResolvedExitProfile:
    """Frozen pre-redesign behavior for open trades that have no snapshot."""
    profiles = {
        "intraday": ("legacy_intraday", 1.5, 0.50, 3.0, 900, 4, 1.0, 6),
        "swing": ("legacy_swing", 2.0, 0.50, 3.0, 3_600, 24, 1.0, 48),
        "bear_rally": ("legacy_bear_rally", 1.0, 0.50, 2.5, 900, 2, 1.0, 6),
    }
    label, target_r, fraction, atr_mult, trail_seconds, stale_hours, stale_r, hard_hours = (
        profiles.get(strategy, profiles["intraday"])
    )
    return ResolvedExitProfile(
        label=label,
        mode=ExitMode.PARTIAL_TRAIL,
        take_profit_pct=0.06,
        stop_loss_pct=0.03,
        time_stop_hours=hard_hours,
        exit_style="atr",
        atr_take_profit_mult=2.0,
        atr_stop_loss_mult=1.0,
        atr_min_stop_pct=0.008,
        atr_max_stop_pct=0.15,
        advanced_exit_enabled=True,
        partial_take_profit_fraction=fraction,
        partial_take_profit_r=target_r,
        chandelier_atr_mult=atr_mult,
        trail_granularity_seconds=trail_seconds,
        stale_time_stop_hours=stale_hours,
        stale_mfe_r=stale_r,
    )


def initial_levels(
    entry_price: float,
    structure_stop: float,
    profile_value: Any,
    *,
    atr_pct: float = 0.0,
    candle_granularity_seconds: int = 3_600,
    assumed_fee_pct_per_side: float = 0.0,
) -> InitialLevels:
    """Calculate entry levels without market or persistence dependencies."""
    profile = resolve_profile(profile_value)
    if 0 < structure_stop < entry_price:
        stop = round(structure_stop, 8)
        target = round(entry_price + (entry_price - stop) * profile.partial_take_profit_r, 8)
        return InitialLevels(
            target,
            stop,
            f"structure stop={(entry_price - stop) / entry_price:.2%} "
            f"partial={profile.partial_take_profit_r:.2f}R",
        )

    take_pct = profile.take_profit_pct
    stop_pct = profile.stop_loss_pct
    note = "fixed"
    if profile.exit_style == "atr" and atr_pct > 0:
        bars = max(profile.time_stop_hours * 3_600 / candle_granularity_seconds, 1.0)
        horizon_vol = atr_pct * math.sqrt(bars)
        stop_pct = max(
            profile.atr_min_stop_pct,
            min(horizon_vol * profile.atr_stop_loss_mult, profile.atr_max_stop_pct),
        )
        reward_risk = profile.atr_take_profit_mult / max(profile.atr_stop_loss_mult, 1e-12)
        take_pct = stop_pct * reward_risk
        note = f"atr={atr_pct:.2%}/bar horizon={horizon_vol:.2%}"
    elif profile.exit_style == "atr":
        note = "fixed (no ATR history)"
    fee_floor = 3.0 * assumed_fee_pct_per_side
    if take_pct < fee_floor:
        take_pct = fee_floor
        note += " tp raised to fee floor"
    return InitialLevels(
        round(entry_price * (1.0 + take_pct), 8),
        round(entry_price * (1.0 - stop_pct), 8),
        f"{note} tp={take_pct:.2%} sl={stop_pct:.2%}",
    )


def active_stop(stop_loss: float, trailing_stop: float, partial_taken: bool) -> float:
    return max(stop_loss, trailing_stop if partial_taken else 0.0)


def fee_aware_breakeven(
    entry_price: float,
    remaining_entry_fee_per_unit: float,
    modeled_sell_discount: float,
) -> float:
    """Reference price needed to recover remaining basis after modeled sell costs."""
    if modeled_sell_discount <= 0:
        raise ValueError("modeled sell discount must be positive")
    return (entry_price + remaining_entry_fee_per_unit) / modeled_sell_discount


def chandelier_ratchet(
    current_stop: float,
    structural_stop: float,
    highest_price: float,
    atr_absolute: float,
    atr_multiplier: float,
) -> float:
    if atr_absolute <= 0:
        return max(current_stop, structural_stop)
    return max(
        current_stop,
        structural_stop,
        highest_price - atr_multiplier * atr_absolute,
    )


def first_partial_quantity(original_qty: float, current_qty: float, fraction: float) -> float:
    return min(max(original_qty * fraction, 0.0), current_qty)


def first_partial_economics(
    *,
    entry_price: float,
    target_price: float,
    original_qty: float,
    current_qty: float,
    fraction: float,
    total_entry_fee: float,
    modeled_exit_net_proceeds: float,
) -> FirstPartialEconomics:
    qty = first_partial_quantity(original_qty, current_qty, fraction)
    entry_fee_share = total_entry_fee * (qty / original_qty) if original_qty > 0 else 0.0
    gross = (target_price - entry_price) * qty
    exit_cost = target_price * qty - modeled_exit_net_proceeds
    return FirstPartialEconomics(
        qty, gross, entry_fee_share, exit_cost, gross - entry_fee_share - exit_cost
    )


def mfe_r(highest_price: float, entry_price: float, initial_risk_per_unit: float) -> float:
    if initial_risk_per_unit <= 0:
        return 0.0
    return max(highest_price - entry_price, 0.0) / initial_risk_per_unit


def time_exit_reason(
    profile_value: Any,
    *,
    held_seconds: float,
    highest_price: float,
    entry_price: float,
    initial_risk_per_unit: float,
    partial_taken: bool,
) -> str | None:
    profile = resolve_profile(profile_value)
    if (
        profile.advanced_exit_enabled
        and not partial_taken
        and held_seconds >= profile.stale_time_stop_hours * 3_600
        and mfe_r(highest_price, entry_price, initial_risk_per_unit) < profile.stale_mfe_r
    ):
        return "STALE_TIME_STOP"
    if held_seconds >= profile.time_stop_hours * 3_600:
        return "TIME_STOP"
    return None


def quote_step(
    profile_value: Any,
    *,
    price: float,
    stop_loss: float,
    take_profit: float,
    highest_price: float,
    trailing_stop: float,
    partial_taken: bool,
    original_qty: float,
    current_qty: float,
    atr_absolute: float = 0.0,
) -> PolicyStep:
    """Step one quote; price protection is always evaluated before upside."""
    profile = resolve_profile(profile_value)
    stop = active_stop(stop_loss, trailing_stop, partial_taken)
    if price <= stop:
        reason = "TRAILING_STOP" if partial_taken and stop > stop_loss else "STOP_LOSS"
        return PolicyStep(
            (ExitAction(ExitActionKind.CLOSE, reason, price, current_qty),), highest_price, stop
        )
    highest = max(highest_price, price)
    if not profile.advanced_exit_enabled and price >= take_profit:
        return PolicyStep(
            (ExitAction(ExitActionKind.CLOSE, "TAKE_PROFIT", take_profit, current_qty),),
            highest,
            trailing_stop,
        )
    if profile.advanced_exit_enabled and not partial_taken and price >= take_profit:
        qty = first_partial_quantity(
            original_qty, current_qty, profile.partial_take_profit_fraction
        )
        return PolicyStep(
            (ExitAction(ExitActionKind.PARTIAL, "PARTIAL", take_profit, qty),),
            highest,
            trailing_stop,
        )
    if profile.advanced_exit_enabled and partial_taken:
        stop = chandelier_ratchet(
            trailing_stop,
            stop_loss,
            highest,
            atr_absolute,
            profile.chandelier_atr_mult,
        )
        if price <= stop:
            return PolicyStep(
                (ExitAction(ExitActionKind.CLOSE, "TRAILING_STOP", price, current_qty),),
                highest,
                stop,
            )
        return PolicyStep((), highest, stop)
    return PolicyStep((), highest, trailing_stop)


def bar_step(
    profile_value: Any,
    *,
    low: float,
    high: float,
    stop_loss: float,
    take_profit: float,
    highest_price: float,
    trailing_stop: float,
    partial_taken: bool,
    original_qty: float,
    current_qty: float,
    atr_absolute: float = 0.0,
    post_partial_stop: float | None = None,
) -> PolicyStep:
    """Step an OHLC bar conservatively: every pre-existing stop wins ambiguity."""
    profile = resolve_profile(profile_value)
    stop = active_stop(stop_loss, trailing_stop, partial_taken)
    if low <= stop:
        reason = "TRAILING_STOP" if partial_taken and stop > stop_loss else "STOP_LOSS"
        return PolicyStep(
            (ExitAction(ExitActionKind.CLOSE, reason, stop, current_qty),), highest_price, stop
        )

    highest = max(highest_price, high)
    if not profile.advanced_exit_enabled:
        actions = (
            (ExitAction(ExitActionKind.CLOSE, "TAKE_PROFIT", take_profit, current_qty),)
            if high >= take_profit
            else ()
        )
        return PolicyStep(actions, highest, trailing_stop)

    actions: list[ExitAction] = []
    is_partial = partial_taken
    remaining = current_qty
    stop = trailing_stop
    if not is_partial and high >= take_profit:
        quantity = first_partial_quantity(
            original_qty, current_qty, profile.partial_take_profit_fraction
        )
        actions.append(ExitAction(ExitActionKind.PARTIAL, "PARTIAL", take_profit, quantity))
        remaining -= quantity
        is_partial = True
        if post_partial_stop is not None:
            stop = max(stop_loss, post_partial_stop)
    if is_partial:
        stop = chandelier_ratchet(
            stop,
            stop_loss,
            highest,
            atr_absolute,
            profile.chandelier_atr_mult,
        )
        if low <= stop:
            actions.append(ExitAction(ExitActionKind.CLOSE, "TRAILING_STOP", stop, remaining))
    return PolicyStep(tuple(actions), highest, stop)

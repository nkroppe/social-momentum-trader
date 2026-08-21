"""Deterministic replay adapter for the production exit policy.

This repository does not contain a historical entry backtester. The adapter
keeps exit replay on exactly the same pure state machine used by PAPER.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .market import Candle
from .trader.exit_policy import ExitAction, ExitPlan, ExitState, step_bar


@dataclass(frozen=True)
class ExitReplayResult:
    actions: tuple[ExitAction, ...]
    state: ExitState


def replay_exit_bars(
    *,
    plan: ExitPlan,
    initial_state: ExitState,
    bars: Iterable[tuple[datetime, Candle, float]],
) -> ExitReplayResult:
    """Replay timestamped bars and ATR values until the position closes."""
    state = initial_state
    actions: list[ExitAction] = []
    for now, bar, atr_abs in bars:
        step = step_bar(state=state, plan=plan, bar=bar, atr_abs=atr_abs, now=now)
        action = step.action
        if action.kind != "none":
            actions.append(action)
        if action.kind == "close":
            state = ExitState(
                entry_price=state.entry_price,
                qty=0.0,
                original_qty=state.original_qty,
                highest_price=step.highest_price,
                partial_taken=state.partial_taken,
                trailing_stop=step.trailing_stop,
            )
            break
        qty = state.qty - action.qty if action.kind == "partial" else state.qty
        state = ExitState(
            entry_price=state.entry_price,
            qty=qty,
            original_qty=state.original_qty,
            highest_price=step.highest_price,
            partial_taken=state.partial_taken or action.kind == "partial",
            trailing_stop=step.trailing_stop,
        )
    return ExitReplayResult(actions=tuple(actions), state=state)

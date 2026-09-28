"""Go-live edge gate: require net-positive closed trades on the active policy."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..models import Trade, TradeStatus
from ..store import Store


@dataclass(frozen=True)
class EdgeGateResult:
    passed: bool
    net_pnl: float
    n: int
    fingerprint: str

    @property
    def detail(self) -> str:
        prefix = (self.fingerprint or "")[:12]
        return (
            f"net ${self.net_pnl:,.2f} over n={self.n} closed trades on fp {prefix} (need > $0)"
        )


def evaluate_edge_gate(trades: Iterable[Trade], fingerprint: str) -> EdgeGateResult:
    """Score CLOSED trades stamped with ``fingerprint`` (full-string equality).

    An empty fingerprint is always a fail with n=0, even if trades are present.
    ``passed`` requires at least one matching closed trade and net realized P/L
    strictly greater than zero. ``Trade.realized_pnl`` is already net of fees.
    """
    if not fingerprint:
        return EdgeGateResult(passed=False, net_pnl=0.0, n=0, fingerprint=fingerprint)

    matched = [
        trade
        for trade in trades
        if trade.status == TradeStatus.CLOSED and trade.config_fingerprint == fingerprint
    ]
    n = len(matched)
    net_pnl = sum(float(trade.realized_pnl) for trade in matched)
    return EdgeGateResult(
        passed=n > 0 and net_pnl > 0.0,
        net_pnl=net_pnl,
        n=n,
        fingerprint=fingerprint,
    )


def live_edge_gate(store: Store, fingerprint: str) -> EdgeGateResult:
    """Evaluate the go-live edge gate against ``store.closed_trades()``."""
    if not fingerprint:
        return evaluate_edge_gate((), fingerprint)
    return evaluate_edge_gate(store.closed_trades(), fingerprint)

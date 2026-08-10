"""Deterministic, network-free price-only candle replay."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import (
    CONFIG_DIR,
    MarketConfig,
    RiskConfig,
    SignalsConfig,
    StrategyConfig,
    UniverseConfig,
    get_market,
    get_risk,
    get_settings,
    get_signals,
    get_strategies,
    get_universe,
)
from .market import Candle, atr, sma, trailing_return, volume_zscore
from .ops.performance import EquityPoint, PerformanceTrade, calculate_performance
from .trader.signals import PriceSetup, detect_price_setup

CSV_FIELDS = ("timestamp", "open", "high", "low", "close", "volume")
ARTIFACTS = (
    "manifest.json",
    "opportunities.csv",
    "opportunities.jsonl",
    "trades.csv",
    "equity_curve.csv",
    "summary.json",
)
BACKTEST_VERSION = 1


class BacktestDataError(ValueError):
    """Local replay data is malformed or cannot support aligned replay."""


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> int:
    """Parse an epoch or explicitly timezone-qualified ISO-8601 timestamp."""
    text = value.strip()
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        if not math.isfinite(numeric):
            raise BacktestDataError(f"invalid timestamp {value!r}")
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        return int(numeric)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BacktestDataError(f"invalid timestamp {value!r}") from exc
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise BacktestDataError(f"timestamp must be UTC (Z or +00:00): {value!r}")
    return int(parsed.astimezone(UTC).timestamp())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_candle_csv(path: Path) -> tuple[list[Candle], int]:
    """Read one strict UTC OHLCV file and infer its exact base granularity."""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != CSV_FIELDS:
                raise BacktestDataError(
                    f"{path.name}: header must be exactly {','.join(CSV_FIELDS)}"
                )
            candles: list[Candle] = []
            previous: int | None = None
            for line, row in enumerate(reader, 2):
                try:
                    ts = parse_utc(row["timestamp"])
                    values = [float(row[name]) for name in CSV_FIELDS[1:]]
                except (KeyError, TypeError, ValueError) as exc:
                    raise BacktestDataError(f"{path.name}:{line}: malformed OHLCV row") from exc
                if not all(math.isfinite(value) for value in values):
                    raise BacktestDataError(f"{path.name}:{line}: non-finite OHLCV value")
                open_, high, low, close, volume = values
                if (
                    min(open_, high, low, close) <= 0
                    or volume < 0
                    or low > min(open_, close)
                    or high < max(open_, close)
                    or low > high
                ):
                    raise BacktestDataError(f"{path.name}:{line}: invalid OHLCV candle")
                if previous is not None and ts <= previous:
                    kind = "duplicate" if ts == previous else "not sorted"
                    raise BacktestDataError(f"{path.name}:{line}: timestamp is {kind}")
                candles.append(Candle(ts, low, high, open_, close, volume))
                previous = ts
    except OSError as exc:
        raise BacktestDataError(f"cannot read {path}: {exc}") from exc
    if len(candles) < 2:
        raise BacktestDataError(f"{path.name}: need at least two candles")
    granularity = candles[1].ts - candles[0].ts
    if granularity <= 0:
        raise BacktestDataError(f"{path.name}: invalid candle granularity")
    for left, right in zip(candles, candles[1:], strict=False):
        if left.ts % granularity:
            raise BacktestDataError(f"{path.name}: unaligned candle at {left.ts}")
        if right.ts != left.ts + granularity:
            raise BacktestDataError(f"{path.name}: candle gap after {left.ts}")
    if candles[-1].ts % granularity:
        raise BacktestDataError(f"{path.name}: unaligned candle at {candles[-1].ts}")
    return candles, granularity


def _completed_aggregate(
    candles: list[Candle], base_seconds: int, target_seconds: int, as_of: int
) -> list[Candle]:
    if target_seconds < base_seconds or target_seconds % base_seconds:
        raise BacktestDataError(
            f"source granularity {base_seconds}s cannot build {target_seconds}s candles"
        )
    expected = target_seconds // base_seconds
    buckets: dict[int, list[Candle]] = {}
    for candle in candles:
        if candle.ts + base_seconds > as_of:
            break
        bucket = candle.ts // target_seconds * target_seconds
        buckets.setdefault(bucket, []).append(candle)
    output: list[Candle] = []
    for ts, rows in sorted(buckets.items()):
        if ts + target_seconds > as_of or len(rows) != expected:
            continue
        if rows[0].ts != ts or rows[-1].ts + base_seconds != ts + target_seconds:
            continue
        output.append(
            Candle(
                ts=ts,
                open=rows[0].open,
                high=max(row.high for row in rows),
                low=min(row.low for row in rows),
                close=rows[-1].close,
                volume=sum(row.volume for row in rows),
            )
        )
    return output


@dataclass
class PendingEntry:
    opportunity: dict[str, Any]
    setup: PriceSetup
    strategy: StrategyConfig
    ticker: str
    product_id: str
    tier: str
    decision_time: int


@dataclass
class Position:
    trade_id: str
    ticker: str
    product_id: str
    tier: str
    strategy: StrategyConfig
    setup_name: str
    opened_at: int
    entry_reference: float
    entry_price: float
    entry_notional: float
    original_qty: float
    qty: float
    stop: float
    target: float
    initial_risk: float
    entry_fee: float
    fees: float
    slippage: float
    highest: float
    partial_taken: bool = False
    gross_pnl: float = 0.0
    exit_notional: float = 0.0
    exit_value: float = 0.0
    exit_qty: float = 0.0

    @property
    def active_stop(self) -> float:
        return self.stop


@dataclass
class BacktestResult:
    manifest: dict[str, Any]
    opportunities: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    equity_curve: list[dict[str, Any]]
    summary: dict[str, Any]


@dataclass
class BacktestEngine:
    data: dict[str, list[Candle]]
    base_seconds: int
    products: dict[str, str]
    tiers: dict[str, str]
    strategies: list[StrategyConfig]
    market: MarketConfig
    risk: RiskConfig
    signals: SignalsConfig
    initial_equity: float
    start: int
    end: int
    opportunities: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    curve: list[dict[str, Any]] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    pending: list[PendingEntry] = field(default_factory=list)
    closed_pnl_by_strategy: dict[str, float] = field(default_factory=dict)
    cooldowns: dict[tuple[str, str], int] = field(default_factory=dict)
    entries: list[tuple[int, str]] = field(default_factory=list)
    daily_equity_baselines: dict[tuple[str, str], float] = field(default_factory=dict)
    weekly_equity_baselines: dict[tuple[str, int, int], float] = field(default_factory=dict)
    cash: float = 0.0

    def __post_init__(self) -> None:
        self.cash = self.initial_equity
        self.strategies.sort(key=lambda row: row.name)
        needed = {
            self.market.candle_granularity_seconds,
            self.market.regime.granularity_seconds,
            *(st.entry.trigger_granularity_seconds for st in self.strategies),
            *(st.entry.bias_granularity_seconds for st in self.strategies),
        }
        for seconds in needed:
            if seconds < self.base_seconds or seconds % self.base_seconds:
                raise BacktestDataError(
                    f"source granularity {self.base_seconds}s cannot build required {seconds}s"
                )

    @property
    def slip(self) -> float:
        return self.market.paper_adverse_slippage_bps / 10_000.0

    def _candles(self, ticker: str, seconds: int, as_of: int) -> list[Candle]:
        return _completed_aggregate(self.data[ticker], self.base_seconds, seconds, as_of)

    def _marks(self, index: int, *, use_open: bool = False) -> dict[str, float]:
        return {
            ticker: (rows[index].open if use_open else rows[index].close)
            for ticker, rows in self.data.items()
        }

    def _position_net(self, position: Position, mark: float) -> float:
        remaining_gross = (mark - position.entry_reference) * position.qty
        return position.gross_pnl + remaining_gross - position.fees - position.slippage

    def _strategy_equity(self, name: str, marks: dict[str, float]) -> float:
        strategy = next(row for row in self.strategies if row.name == name)
        value = self.initial_equity * strategy.allocation
        value += self.closed_pnl_by_strategy.get(name, 0.0)
        value += sum(
            self._position_net(position, marks[position.ticker])
            for position in self.positions
            if position.strategy.name == name
        )
        return value

    def _equity(self, marks: dict[str, float]) -> float:
        return self.cash + sum(position.qty * marks[position.ticker] for position in self.positions)

    def _regime(self, as_of: int) -> tuple[bool, str]:
        cfg = self.market.regime
        if not cfg.enabled:
            return True, "disabled"
        ticker = next(
            (
                ticker
                for ticker, product in self.products.items()
                if product == cfg.benchmark_product_id
            ),
            None,
        )
        if ticker is None:
            return False, f"benchmark {cfg.benchmark_product_id} not selected"
        rows = self._candles(ticker, cfg.granularity_seconds, as_of)
        if len(rows) < cfg.sma_periods:
            return False, "insufficient benchmark history"
        average = sma(rows, cfg.sma_periods)
        return rows[-1].close > average, (
            f"{rows[-1].close:.8f} {'above' if rows[-1].close > average else 'below'} "
            f"SMA{cfg.sma_periods} {average:.8f}"
        )

    def _confirmation(
        self, ticker: str, strategy: StrategyConfig, tier: str, as_of: int
    ) -> tuple[bool, str]:
        cfg = self.market.confirmation
        if not cfg.enabled:
            return True, "disabled"
        rows = self._candles(ticker, self.market.candle_granularity_seconds, as_of)
        periods = max(
            1,
            strategy.confirm_lookback_hours * 3600 // self.market.candle_granularity_seconds,
        )
        needed = max(cfg.sma_periods, cfg.volume_periods + 1, periods + 1)
        if len(rows) < needed:
            return False, "insufficient confirmation history"
        latest = rows[-1]
        average = sma(rows, cfg.sma_periods)
        if cfg.require_above_sma and latest.close <= average:
            return False, "below confirmation SMA"
        vol_z = volume_zscore(rows, cfg.volume_periods)
        if vol_z < cfg.min_volume_zscore:
            return False, "confirmation volume rejected"
        floor = max(
            strategy.confirm_min_return_pct,
            self.signals.tier(tier).min_trailing_return_pct,
        )
        ret = trailing_return(rows, periods)
        if cfg.require_positive_return and ret < floor:
            return False, "confirmation return rejected"
        return True, f"sma={average:.8f} volume_z={vol_z:.6f} return={ret:.8f}"

    def _opportunity(
        self, ticker: str, strategy: StrategyConfig, as_of: int
    ) -> PendingEntry | None:
        product = self.products[ticker]
        tier_name = self.tiers[ticker]
        trigger_seconds = strategy.entry.trigger_granularity_seconds
        trigger = self._candles(ticker, trigger_seconds, as_of)
        bias = self._candles(ticker, strategy.entry.bias_granularity_seconds, as_of)
        trigger_ts = trigger[-1].ts if trigger else as_of - trigger_seconds
        identity = f"{BACKTEST_VERSION}|{strategy.name}|{ticker}|{trigger_ts}"
        row: dict[str, Any] = {
            "opportunity_id": hashlib.sha256(identity.encode()).hexdigest()[:32],
            "decision_time": _iso(as_of),
            "trigger_timestamp": _iso(trigger_ts),
            "strategy": strategy.name,
            "ticker": ticker,
            "product_id": product,
            "tier": tier_name,
            "status": "no_setup",
            "reason": "",
            "setup": "",
            "signal_close": trigger[-1].close if trigger else 0.0,
            "entry_time": "",
            "entry_price": "",
            "stop_price": "",
            "target_price": "",
            "notional": "",
        }
        self.opportunities.append(row)
        regime_ok, reason = self._regime(as_of)
        if not regime_ok:
            row["status"] = "regime_blocked"
            row["reason"] = reason
            return None
        needed = max(51, strategy.entry.breakout_lookback + strategy.entry.retest_window + 2)
        if len(trigger) < needed or len(bias) < 50:
            row["status"] = "insufficient_data"
            row["reason"] = f"trigger={len(trigger)} bias={len(bias)}"
            return None
        setup = detect_price_setup(
            trigger,
            bias,
            strategy.entry,
            self.signals.tier(tier_name),
            strategy.name,
        )
        if setup is None:
            row["reason"] = "no qualifying deterministic price setup"
            return None
        confirmation_ok, reason = self._confirmation(ticker, strategy, tier_name, as_of)
        row["setup"] = setup.name
        row["stop_price"] = setup.structure_stop
        if not confirmation_ok:
            row["status"] = "confirmation_reject"
            row["reason"] = reason
            return None
        row["status"] = "pending_entry"
        row["reason"] = "price-only setup confirmed"
        return PendingEntry(row, setup, strategy, ticker, product, tier_name, as_of)

    def _global_rejection(
        self,
        pending: PendingEntry,
        notional: float,
        fill_price: float,
        qty: float,
        marks: dict[str, float],
    ) -> str:
        equity = self._equity(marks)
        gross = sum(position.qty * marks[position.ticker] for position in self.positions)
        symbol = sum(
            position.qty * marks[position.ticker]
            for position in self.positions
            if position.ticker == pending.ticker
        )
        micro = sum(
            position.qty * marks[position.ticker]
            for position in self.positions
            if position.tier == "micro"
        )
        heat = sum(
            max(marks[position.ticker] - position.active_stop, 0.0) * position.qty
            + position.active_stop * position.qty * (self.slip + self.risk.assumed_fee_pct_per_side)
            for position in self.positions
        )
        proposed_heat = (
            (fill_price - pending.setup.structure_stop) * qty
            + notional * pending.strategy.assumed_fee_pct_per_side
            + pending.setup.structure_stop
            * qty
            * (self.slip + pending.strategy.assumed_fee_pct_per_side)
        )
        proposed_exposure = fill_price * qty
        checks = (
            ("aggregate open heat", heat + proposed_heat, self.risk.max_aggregate_open_heat_pct),
            ("gross exposure", gross + proposed_exposure, self.risk.max_gross_exposure_pct),
            (
                "combined symbol exposure",
                symbol + proposed_exposure,
                self.risk.max_combined_symbol_exposure_pct,
            ),
            (
                "aggregate micro exposure",
                micro + (proposed_exposure if pending.tier == "micro" else 0.0),
                self.risk.max_micro_exposure_pct,
            ),
        )
        for label, projected, fraction in checks:
            if projected > equity * fraction + 1e-9:
                return f"global {label} cap"
        return ""

    def _execute_pending(self, ts: int, index: int) -> None:
        due = sorted(
            (entry for entry in self.pending if entry.decision_time == ts),
            key=lambda entry: (entry.strategy.name, entry.ticker),
        )
        self.pending = [entry for entry in self.pending if entry.decision_time != ts]
        marks = self._marks(index, use_open=True)
        for entry in due:
            row = entry.opportunity
            strategy = entry.strategy
            reference = self.data[entry.ticker][index].open
            if reference <= entry.setup.structure_stop:
                row["status"] = "entry_rejected"
                row["reason"] = "next bar opened at/below structure stop"
                continue
            move = abs(reference - entry.setup.entry_price) / entry.setup.entry_price
            if move > strategy.entry.max_entry_slippage_pct:
                row["status"] = "entry_rejected"
                row["reason"] = "next-bar setup stale"
                continue
            if any(
                position.ticker == entry.ticker and position.strategy.name == strategy.name
                for position in self.positions
            ):
                row["status"] = "risk_rejected"
                row["reason"] = "position already open"
                continue
            open_for_strategy = sum(
                position.strategy.name == strategy.name for position in self.positions
            )
            if open_for_strategy >= strategy.max_open_positions:
                row["status"] = "risk_rejected"
                row["reason"] = "max open positions"
                continue
            recent = [
                opened
                for opened, name in self.entries
                if name == strategy.name and opened > ts - 86_400
            ]
            if len(recent) >= strategy.max_trades_per_day:
                row["status"] = "risk_rejected"
                row["reason"] = "max trades per rolling day"
                continue
            cooldown = self.cooldowns.get((strategy.name, entry.ticker), 0)
            if ts < cooldown:
                row["status"] = "risk_rejected"
                row["reason"] = "stop cooldown"
                continue
            allocation_equity = self._strategy_equity(strategy.name, marks)
            current = datetime.fromtimestamp(ts, UTC)
            day_key = (strategy.name, current.date().isoformat())
            iso_year, iso_week, _ = current.isocalendar()
            week_key = (strategy.name, iso_year, iso_week)
            day_baseline = self.daily_equity_baselines.setdefault(day_key, allocation_equity)
            week_baseline = self.weekly_equity_baselines.setdefault(week_key, allocation_equity)
            if (
                day_baseline > 0
                and (allocation_equity - day_baseline) / day_baseline
                <= strategy.daily_loss_halt_pct
            ):
                row["status"] = "risk_rejected"
                row["reason"] = "daily marked-equity loss halt"
                continue
            if (
                week_baseline > 0
                and (allocation_equity - week_baseline) / week_baseline
                <= strategy.weekly_loss_halt_pct
            ):
                row["status"] = "risk_rejected"
                row["reason"] = "weekly marked-equity loss halt"
                continue

            fill_price = reference * (1.0 + self.slip)
            stop_pct = (fill_price - entry.setup.structure_stop) / fill_price
            risk_notional = (
                allocation_equity * strategy.risk_per_trade_pct / stop_pct if stop_pct > 0 else 0.0
            )
            tier_mult = min(max(self.signals.tier(entry.tier).max_position_pct_mult, 0.0), 1.0)
            vol_mult = 1.0
            if self.market.sizing.enabled and entry.setup.atr_pct > 0:
                vol_mult = self.market.sizing.target_atr_pct / entry.setup.atr_pct
                vol_mult = min(
                    max(vol_mult, self.market.sizing.min_scale),
                    self.market.sizing.max_scale,
                )
            conviction = min(max(entry.setup.conviction, 0.0), 1.0)
            hard_cap = allocation_equity * strategy.max_position_pct
            notional = round(
                min(risk_notional, hard_cap * tier_mult * vol_mult * conviction, hard_cap),
                2,
            )
            if notional < strategy.min_order_notional_usd:
                row["status"] = "risk_rejected"
                row["reason"] = "below minimum order notional"
                continue
            qty = notional / fill_price
            target = (
                fill_price
                + (fill_price - entry.setup.structure_stop) * strategy.partial_take_profit_r
            )
            partial_qty = qty * strategy.partial_take_profit_fraction
            projected_sell_price = target * (1.0 - self.slip)
            projected_entry_fee = (
                notional * strategy.assumed_fee_pct_per_side * strategy.partial_take_profit_fraction
            )
            projected_exit_fee = (
                projected_sell_price * partial_qty * strategy.assumed_fee_pct_per_side
            )
            projected_gross = (target - fill_price) * partial_qty
            projected_slippage = (target - projected_sell_price) * partial_qty
            projected_net = (
                projected_gross - projected_entry_fee - projected_exit_fee - projected_slippage
            )
            if projected_net <= 0:
                row["status"] = "risk_rejected"
                row["reason"] = (
                    "first partial not positively economic after configured "
                    f"fees/slippage (net={projected_net:.8f})"
                )
                continue
            rejection = self._global_rejection(entry, notional, fill_price, qty, marks)
            if rejection:
                row["status"] = "risk_rejected"
                row["reason"] = rejection
                continue
            fee = notional * strategy.assumed_fee_pct_per_side
            trade_id = hashlib.sha256(f"{row['opportunity_id']}|{ts}".encode()).hexdigest()[:24]
            self.positions.append(
                Position(
                    trade_id=trade_id,
                    ticker=entry.ticker,
                    product_id=entry.product_id,
                    tier=entry.tier,
                    strategy=strategy,
                    setup_name=entry.setup.name,
                    opened_at=ts,
                    entry_reference=reference,
                    entry_price=fill_price,
                    entry_notional=notional,
                    original_qty=qty,
                    qty=qty,
                    stop=entry.setup.structure_stop,
                    target=target,
                    initial_risk=(fill_price - entry.setup.structure_stop) * qty,
                    entry_fee=fee,
                    fees=fee,
                    slippage=(fill_price - reference) * qty,
                    highest=fill_price,
                )
            )
            self.cash -= notional + fee
            self.entries.append((ts, strategy.name))
            row.update(
                status="opened",
                reason="entered at next bar open with adverse slippage",
                entry_time=_iso(ts),
                entry_price=fill_price,
                target_price=target,
                notional=notional,
            )

    def _sell(
        self,
        position: Position,
        qty: float,
        reference: float,
        ts: int,
        reason: str,
        *,
        final: bool,
    ) -> None:
        qty = min(qty, position.qty)
        price = reference * (1.0 - self.slip)
        gross_value = price * qty
        fee = gross_value * position.strategy.assumed_fee_pct_per_side
        self.cash += gross_value - fee
        position.gross_pnl += (reference - position.entry_reference) * qty
        position.slippage += (reference - price) * qty
        position.fees += fee
        position.exit_notional += gross_value
        position.exit_value += price * qty
        position.exit_qty += qty
        position.qty -= qty
        if not final:
            return
        net = position.gross_pnl - position.fees - position.slippage
        record = {
            "trade_id": position.trade_id,
            "strategy": position.strategy.name,
            "ticker": position.ticker,
            "product_id": position.product_id,
            "setup": position.setup_name,
            "opened_at": _iso(position.opened_at),
            "closed_at": _iso(ts),
            "entry_reference": position.entry_reference,
            "entry_price": position.entry_price,
            "exit_price": (position.exit_value / position.exit_qty if position.exit_qty else 0.0),
            "quantity": position.original_qty,
            "entry_notional": position.entry_notional,
            "exit_notional": position.exit_notional,
            "initial_risk": position.initial_risk,
            "gross_pnl": position.gross_pnl,
            "fees": position.fees,
            "modeled_slippage": position.slippage,
            "net_pnl": net,
            "net_r": net / position.initial_risk if position.initial_risk > 0 else 0.0,
            "exit_reason": reason,
        }
        self.trades.append(record)
        self.closed_pnl_by_strategy[position.strategy.name] = (
            self.closed_pnl_by_strategy.get(position.strategy.name, 0.0) + net
        )
        if reason in ("STOP_LOSS", "TRAILING_STOP"):
            self.cooldowns[(position.strategy.name, position.ticker)] = (
                ts + position.strategy.cooldown_minutes_after_stop * 60
            )
        self.positions.remove(position)

    def _manage_positions(self, ts: int, index: int) -> None:
        for position in sorted(
            list(self.positions), key=lambda row: (row.strategy.name, row.ticker)
        ):
            bar = self.data[position.ticker][index]
            strategy = position.strategy
            if bar.low <= position.active_stop:
                reason = "TRAILING_STOP" if position.partial_taken else "STOP_LOSS"
                self._sell(
                    position,
                    position.qty,
                    position.active_stop,
                    ts + self.base_seconds,
                    reason,
                    final=True,
                )
                continue
            position.highest = max(position.highest, bar.high)
            advanced = strategy.advanced_exit_enabled
            if advanced and not position.partial_taken and bar.high >= position.target:
                partial = position.original_qty * strategy.partial_take_profit_fraction
                self._sell(
                    position,
                    partial,
                    position.target,
                    ts + self.base_seconds,
                    "PARTIAL",
                    final=False,
                )
                position.partial_taken = True
                remaining_entry_fee = position.entry_fee * (position.qty / position.original_qty)
                fee_per_unit = remaining_entry_fee / position.qty
                breakeven = (position.entry_price + fee_per_unit) / (
                    (1.0 - self.slip) * (1.0 - strategy.assumed_fee_pct_per_side)
                )
                position.stop = max(position.stop, breakeven)
            elif not advanced and bar.high >= position.target:
                self._sell(
                    position,
                    position.qty,
                    position.target,
                    ts + self.base_seconds,
                    "TAKE_PROFIT",
                    final=True,
                )
                continue
            if advanced and position.partial_taken:
                trigger = self._candles(
                    position.ticker,
                    strategy.entry.trigger_granularity_seconds,
                    ts + self.base_seconds,
                )
                atr_abs = atr(trigger, self.market.atr_periods)
                if atr_abs > 0:
                    position.stop = max(
                        position.stop,
                        position.highest - strategy.chandelier_atr_mult * atr_abs,
                    )
                if bar.low <= position.stop:
                    self._sell(
                        position,
                        position.qty,
                        position.stop,
                        ts + self.base_seconds,
                        "TRAILING_STOP",
                        final=True,
                    )
                    continue
            held = ts + self.base_seconds - position.opened_at
            stale = strategy.stale_time_stop_hours * 3600
            one_r = position.entry_price + (position.initial_risk / position.original_qty)
            if (
                advanced
                and not position.partial_taken
                and held >= stale
                and position.highest < one_r
            ):
                self._sell(
                    position,
                    position.qty,
                    bar.close,
                    ts + self.base_seconds,
                    "STALE_TIME_STOP",
                    final=True,
                )
                continue
            if held >= strategy.time_stop_hours * 3600:
                self._sell(
                    position,
                    position.qty,
                    bar.close,
                    ts + self.base_seconds,
                    "TIME_STOP",
                    final=True,
                )

    def _evaluate_close(self, as_of: int) -> None:
        for strategy in self.strategies:
            granularity = strategy.entry.trigger_granularity_seconds
            if as_of % granularity:
                continue
            for ticker in sorted(self.data):
                pending = self._opportunity(ticker, strategy, as_of)
                if pending is not None:
                    self.pending.append(pending)

    def run(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        timestamps = [row.ts for row in next(iter(self.data.values()))]
        for index, ts in enumerate(timestamps):
            if ts < self.start or ts >= self.end:
                continue
            self._execute_pending(ts, index)
            self._manage_positions(ts, index)
            marks = self._marks(index)
            equity = self._equity(marks)
            exposure = sum(position.qty * marks[position.ticker] for position in self.positions)
            self.curve.append(
                {
                    "timestamp": _iso(ts + self.base_seconds),
                    "equity": equity,
                    "cash": self.cash,
                    "gross_exposure": exposure,
                    "exposure_pct": exposure / equity if equity > 0 else 0.0,
                }
            )
            if ts + self.base_seconds < self.end:
                self._evaluate_close(ts + self.base_seconds)
        if self.positions:
            final_index = max(index for index, ts in enumerate(timestamps) if ts < self.end)
            final_ts = timestamps[final_index] + self.base_seconds
            for position in sorted(
                list(self.positions), key=lambda row: (row.strategy.name, row.ticker)
            ):
                self._sell(
                    position,
                    position.qty,
                    self.data[position.ticker][final_index].close,
                    final_ts,
                    "END_OF_DATA",
                    final=True,
                )
            if self.curve:
                marks = self._marks(final_index)
                self.curve[-1].update(
                    equity=self._equity(marks),
                    cash=self.cash,
                    gross_exposure=0.0,
                    exposure_pct=0.0,
                )
        self.opportunities.sort(
            key=lambda row: (row["decision_time"], row["strategy"], row["ticker"])
        )
        self.trades.sort(key=lambda row: (row["closed_at"], row["strategy"], row["ticker"]))
        return self.opportunities, self.trades, self.curve


def _baseline_summary(
    data: dict[str, list[Candle]], start: int, end: int
) -> dict[str, dict[str, float | str]]:
    usable = {
        ticker: [row for row in rows if start <= row.ts < end] for ticker, rows in data.items()
    }
    buy_hold = {
        ticker: (rows[-1].close / rows[0].open - 1.0 if rows else 0.0)
        for ticker, rows in usable.items()
    }
    btc_ticker = next((ticker for ticker in usable if ticker.upper() == "BTC"), None)
    breakout_returns: list[float] = []
    for ticker in sorted(usable):
        rows = usable[ticker]
        index = 21
        while index < len(rows):
            prior_high = max(row.high for row in rows[index - 21 : index - 1])
            if rows[index - 1].close > prior_high:
                exit_index = min(index + 20, len(rows) - 1)
                breakout_returns.append(rows[exit_index].close / rows[index].open - 1.0)
                index = exit_index + 1
            else:
                index += 1
    return {
        "simple_breakout": {
            "return": (sum(breakout_returns) / len(breakout_returns) if breakout_returns else 0.0),
            "trades": float(len(breakout_returns)),
            "definition": "20-bar close breakout, next-open entry, 20-bar hold",
        },
        "btc_buy_hold": {
            "return": buy_hold.get(btc_ticker, 0.0) if btc_ticker else 0.0,
            "definition": "BTC first-open to final-close",
        },
        "equal_weight_buy_hold": {
            "return": sum(buy_hold.values()) / len(buy_hold) if buy_hold else 0.0,
            "definition": "equal-weight first-open to final-close",
        },
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: Iterable[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(fields), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _resolve_inputs(
    data_dir: Path, symbols: list[str] | None, universe: UniverseConfig
) -> tuple[dict[str, Path], dict[str, str], dict[str, str]]:
    requested = [symbol.upper() for symbol in (symbols or sorted(universe.symbols))]
    paths: dict[str, Path] = {}
    products: dict[str, str] = {}
    tiers: dict[str, str] = {}
    product_to_ticker = {
        spec.product_id.upper(): ticker for ticker, spec in universe.symbols.items()
    }
    for value in requested:
        ticker = value if value in universe.symbols else product_to_ticker.get(value)
        if ticker is None:
            raise BacktestDataError(f"unknown symbol/product {value}")
        spec = universe.symbols[ticker]
        path = data_dir / f"{spec.product_id}.csv"
        if not path.exists():
            alternative = data_dir / f"{ticker}.csv"
            path = alternative if alternative.exists() else path
        if not path.is_file():
            if symbols is None:
                continue
            raise BacktestDataError(f"missing candle file for {ticker}: {path}")
        paths[ticker] = path
        products[ticker] = spec.product_id
        tiers[ticker] = spec.tier
    if not paths:
        raise BacktestDataError(f"no universe candle CSV files found in {data_dir}")
    return paths, products, tiers


def run_backtest(
    data_dir: Path,
    output_dir: Path,
    *,
    start: str | None = None,
    end: str | None = None,
    symbols: list[str] | None = None,
    universe: UniverseConfig | None = None,
    strategies: list[StrategyConfig] | None = None,
    market: MarketConfig | None = None,
    risk: RiskConfig | None = None,
    signals: SignalsConfig | None = None,
    initial_equity: float | None = None,
) -> BacktestResult:
    """Run a local replay and atomically replace its deterministic artifacts."""
    universe = universe or get_universe()
    strategies = list(strategies or get_strategies().enabled())
    market = market or get_market()
    risk = risk or get_risk()
    signals = signals or get_signals()
    initial_equity = (
        float(initial_equity) if initial_equity is not None else get_settings().paper_start_equity
    )
    paths, products, tiers = _resolve_inputs(data_dir, symbols, universe)
    loaded = {ticker: load_candle_csv(path) for ticker, path in paths.items()}
    granularities = {granularity for _, granularity in loaded.values()}
    if len(granularities) != 1:
        raise BacktestDataError("selected products have different base granularities")
    base_seconds = granularities.pop()
    data = {ticker: rows for ticker, (rows, _) in loaded.items()}
    reference_timestamps = [row.ts for row in next(iter(data.values()))]
    for ticker, rows in data.items():
        if [row.ts for row in rows] != reference_timestamps:
            raise BacktestDataError(f"{ticker}: timestamps are not aligned with other products")

    replay_start = parse_utc(start) if start else reference_timestamps[0]
    replay_end = parse_utc(end) if end else reference_timestamps[-1] + base_seconds
    if replay_start >= replay_end:
        raise BacktestDataError("--start must be before --end")
    if replay_start % base_seconds or replay_end % base_seconds:
        raise BacktestDataError("start/end must align to the source candle granularity")
    if (
        replay_start < reference_timestamps[0]
        or replay_end > reference_timestamps[-1] + base_seconds
    ):
        raise BacktestDataError("requested range falls outside available aligned data")

    engine = BacktestEngine(
        data,
        base_seconds,
        products,
        tiers,
        strategies,
        market,
        risk,
        signals,
        initial_equity,
        replay_start,
        replay_end,
    )
    opportunities, trades, curve = engine.run()
    performance = calculate_performance(
        [
            PerformanceTrade(
                gross_pnl=row["gross_pnl"],
                fees=row["fees"],
                modeled_slippage=row["modeled_slippage"],
                entry_notional=row["entry_notional"],
                exit_notional=row["exit_notional"],
                initial_risk=row["initial_risk"],
                net_pnl=row["net_pnl"],
            )
            for row in trades
        ],
        [
            EquityPoint(
                parse_utc(row["timestamp"]),
                row["equity"],
                row["gross_exposure"],
            )
            for row in curve
        ],
        initial_equity,
    )
    summary: dict[str, Any] = {
        **performance,
        "initial_equity": initial_equity,
        "final_equity": curve[-1]["equity"] if curve else initial_equity,
        "start": _iso(replay_start),
        "end": _iso(replay_end),
        "symbols": sorted(data),
        "benchmarks": _baseline_summary(data, replay_start, replay_end),
    }
    config_files = ("market.yaml", "risk.yaml", "signals.yaml", "strategies.yaml", "universe.yaml")
    config_hashes = {
        name: _sha256(CONFIG_DIR / name) for name in config_files if (CONFIG_DIR / name).is_file()
    }
    semantic_config = {
        "market": market.model_dump(mode="json"),
        "risk": risk.model_dump(mode="json"),
        "signals": signals.model_dump(mode="json"),
        "strategies": [row.model_dump(mode="json") for row in strategies],
        "universe": universe.model_dump(mode="json"),
        "initial_equity": initial_equity,
    }
    config_fingerprint = hashlib.sha256(
        json.dumps(semantic_config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = {
        "backtest_version": BACKTEST_VERSION,
        "config_fingerprint": config_fingerprint,
        "config_hashes": config_hashes,
        "data_hashes": {paths[ticker].name: _sha256(paths[ticker]) for ticker in sorted(paths)},
        "base_granularity_seconds": base_seconds,
        "start": _iso(replay_start),
        "end": _iso(replay_end),
        "symbols": sorted(data),
        "products": {ticker: products[ticker] for ticker in sorted(products)},
        "artifacts": list(ARTIFACTS),
        "network_access": False,
        "social_replay": False,
        "llm_replay": False,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "manifest.json", manifest)
    opportunity_fields = (
        list(opportunities[0])
        if opportunities
        else [
            "opportunity_id",
            "decision_time",
            "trigger_timestamp",
            "strategy",
            "ticker",
            "product_id",
            "tier",
            "status",
            "reason",
            "setup",
            "signal_close",
            "entry_time",
            "entry_price",
            "stop_price",
            "target_price",
            "notional",
        ]
    )
    _write_csv(output_dir / "opportunities.csv", opportunities, opportunity_fields)
    with (output_dir / "opportunities.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for row in opportunities:
            stream.write(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False))
            stream.write("\n")
    trade_fields = (
        list(trades[0])
        if trades
        else [
            "trade_id",
            "strategy",
            "ticker",
            "product_id",
            "setup",
            "opened_at",
            "closed_at",
            "entry_reference",
            "entry_price",
            "exit_price",
            "quantity",
            "entry_notional",
            "exit_notional",
            "initial_risk",
            "gross_pnl",
            "fees",
            "modeled_slippage",
            "net_pnl",
            "net_r",
            "exit_reason",
        ]
    )
    _write_csv(output_dir / "trades.csv", trades, trade_fields)
    _write_csv(
        output_dir / "equity_curve.csv",
        curve,
        ("timestamp", "equity", "cash", "gross_exposure", "exposure_pct"),
    )
    _write_json(output_dir / "summary.json", summary)
    return BacktestResult(manifest, opportunities, trades, curve, summary)

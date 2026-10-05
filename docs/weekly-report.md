# Weekly report

Plain-text digest of trades closed in the scheduled window (Telegram-safe: no
Markdown). Built by `smt.ops.reports.build_weekly_report`. Reporting and
logging only; none of these sections change entries, exits, or size.

## Cost rows

Every `CostStats` row (strategy, setup, notional, ticker, session) prints
gross, fees, net, net/gross win rate, `fee%` of entry notional, **and**
`fees x.xx% ntl` (the same notional ratio, spelled out). `fee/gross` is `n/a`
when gross P/L is <= 0 so a losing or zero-gross week does not show a negative
or huge percentage. `CostStats.fee_to_gross` still holds the raw ratio.

## MFE capture

`MFE capture (realized_R / MFE_R, only MFE_R >= 0.25R; n=.. used, .. excluded): mean .., median ..`

Trades with `MFE_R < 0.25` (`MFE_CAPTURE_MIN_MFE_R`) are dropped from the mean
so a tiny favorable excursion cannot explode `realized_R / MFE_R`. `n excluded`
counts those dropped trades that still had `MFE_R` above `MFE_R_EPSILON`.
Per-trade `trade_mfe_capture` keeps the epsilon floor for other callers.

## Same-ticker cross-sleeve overlap

Groups where two or more trades on the same ticker from **different**
strategies have overlapping open intervals. A group is printed when at least
one member closed in the report window; still-open members stay in the group
(`closed_at is None` runs until report end / now). Each group lists trade ids
with strategy, the overlap interval, combined net P/L of closed members, and
open members marked open. A weekly total sums combined net across groups.

## By entry session (UTC)

Completed-trade rows carry `session=Asia|Europe|US|Late-US` from `opened_at`.
Boundaries are hours `[start, end)` UTC:

- Asia `[00:00, 08:00)`
- Europe `[08:00, 13:00)`
- US `[13:00, 21:00)`
- Late-US `[21:00, 24:00)`

The rollup uses the same cost-row formatting (count, gross, fees, net, net
win%, fee/gross, fees % ntl). The section header repeats the boundaries.

## Entry slippage (forward-only)

`TradeManager.open_position` writes `entry_*` keys into `exit_snapshot` on both
normal OPEN and ENTRY_RISK paths: signal (`candidate.entry_price`), quote
(pre-order `broker.current_price`), fill, and bps/usd vs each reference.
Positive means paid more than the reference. Never raises; never changes a
trading decision.

The report prints n, mean/median bps vs signal and vs quote, and total $ vs
signal for closed trades that carry those keys. When none: `n=0 (forward-only
since this ship)`.

An optional **backfill (signal=proposed_entry_price)** line joins closed trades
to `opportunity_decisions.proposed_entry_price` via `trade_id` for historical
fill-vs-signal bps.

## Fee hurdle vs MFE

Every strategy (not only intraday). Each row is labeled with strategy. Week and
since-gen-8 blocks include per-strategy summaries (intraday, swing, …) of
non-starters vs trades with hurdle data.

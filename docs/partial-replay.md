# Partial replay: `smt fetch-candles`

Closed-trade counterfactual replay needs a contiguous UTC OHLCV series around
each fill. Postgres does not store candles, so this command downloads them from
Coinbase's **public** Exchange market-data API and writes one CSV per trade.

```bash
smt fetch-candles [--out-dir data/candles] [--fingerprint-prefix c95a0ad410f4] [--overwrite]
```

- **Read-only.** No orders, no API key, no `Authorization` header, no database
  writes. The process opens `Settings` + `Store` the same way other read-only
  CLI commands do and never constructs a live broker.
- **Which trades.** Closed rows whose `config_fingerprint` starts with the
  prefix (default gen-8 `c95a0ad410f4`). One trade's failure is reported as
  `error` and does not stop the rest. Exit code is 0 unless every matching
  trade errored.
- **Window.** Trail granularity comes from `resolve_profile(exit_snapshot)` or
  `legacy_profile(strategy)`. Start is the opened bar, floored, minus
  `(atr_periods + 5)` bars so ATR can warm up. End is the later of the actual
  close and the hard time-stop, plus two pad bars, capped at the last
  fully-closed bar. The time-stop extension is intentional: a counterfactual
  replay may outlive the real exit.
- **4h bars.** Coinbase does not serve 14400s candles. The fetcher pages 1h
  (`3600`) bars — the largest listed granularity that divides 4h — and keeps
  only complete UTC-aligned buckets.
- **Gaps.** Coinbase omits empty bars. `fill_gaps` inserts flat
  `o=h=l=c=previous close`, `volume=0` placeholders so
  `backtest.load_candle_csv` will accept the file (no gaps, duplicates, or
  unaligned timestamps).
- **Files.** `{out-dir}/trade_{id}_{product_id}_{granularity}.csv` with header
  `timestamp,open,high,low,close,volume` and UTC `Z` timestamps. Existing files
  are left in place unless `--overwrite` is set (`cached`).

Each trade prints one line; a summary follows:

```
fetched N, cached M, errors E -> data/candles
```

# Partial replay: `smt partial-replay`

Counterfactual **post-partial** exits for closed gen-8 trades, using only the
CSVs written by `smt fetch-candles`. Trade parameters (entry, stop, target,
qty, fingerprint, stored realized R) come from the Store.

```bash
smt partial-replay [--candles-dir data/candles] [--fingerprint-prefix c95a0ad410f4]
```

- **Read-only.** No network, no orders, no database writes, no policy changes.
  The process opens `Settings` + `Store` the same way `smt fetch-candles` does.
- **Which trades.** Closed rows whose `config_fingerprint` starts with the
  prefix (default gen-8 `c95a0ad410f4`). Trades that never reach a first
  partial on the CSV, that stop before the target, that stale-stop before the
  target, that have zero initial risk, or that lack a usable CSV are listed as
  skipped with a reason.
- **Bars.** Only bars with `ts + granularity > opened_at` (the bar containing
  entry onward). The entry bar uses its high for partial detection and ignores
  its low (it may predate the fill). No stop check on the entry bar.
- **Partial.** `qty_p = first_partial_quantity(original_qty, original_qty,
  profile.partial_take_profit_fraction)` at `take_profit`. Remaining qty is
  `original_qty - qty_p`.
- **Ambiguity.** In any bar that touches both a stop and a new high, the stop
  fills first at `min(bar.open, stop)` (a gap-open below the stop fills at the
  open).
- **Stale time stop.** Before a first partial, if `advanced_exit_enabled` and
  the bar end reaches `opened_at + stale_time_stop_hours` with
  `mfe_r(highest, entry, initial_risk_per_unit) < stale_mfe_r`, the trade is
  skipped (`stale time stop before target`). Highest starts at `entry_price`
  and is updated with each bar high (including the entry bar) after that bar's
  stop and TP checks. Evaluation order is stop, then TP, then stale, then hard
  time stop.
- **Variant (b) BE+fees.** After the partial, the remaining stop is
  `max(stop_loss, entry + 2 * entry * fee_rate)` and does not move.
- **Variant (c) 2.0x ATR trail.** Remaining stop is
  `max(prev_stop, stop_loss, highest - 2.0 * ATR)`, with ATR from bars up to
  and including the current CSV bar (trail granularity). Highest starts at
  `max(take_profit, partial-bar high)` and only ratchets up. The initial
  post-partial stop is computed on the partial bar the same way; walking
  starts on the next bar.
- **Time stop / data end.** Hard time stop is `opened_at + time_stop_hours`
  (profile). If the bar end reaches it before a stop, the remainder exits at
  that bar's close (`TIME_STOP`). If the CSV ends first, exit at the last
  close (`DATA_END`, flagged).
- **Realized R.** Net P/L after per-side fees on entry (full qty), the
  partial at TP, and the remaining exit, divided by
  `initial_risk_per_unit * original_qty`. Actual R is
  `reports.trade_realized_r` on the stored trade. Fee rate is the strategy's
  `assumed_fee_pct_per_side` (risk config fallback).
- **Output.** Fixed-width table plus a MEAN row, `n=K [small-n]` when
  `n < 5`, and skipped counts by reason.

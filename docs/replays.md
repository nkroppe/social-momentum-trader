# Retrospective replays: `smt replays`

Read-only tables over **closed** trades whose `config_fingerprint` starts with
the gen-8 prefix (`c95a0ad410f4`). The command opens `Settings` + `Store` the
same way other report CLIs do, reads rows, and prints. It never writes the
database, never places orders, and does not change entries, exits, or size.

```bash
docker compose exec trader smt replays
smt replays --json
```

```bash
smt replays [--fingerprint-prefix c95a0ad410f4] [--fee-rates 0.004,0.006,0.009] [--json]
```

## Fee sensitivity

One table. The first row is **All**, then one row per setup (opportunity
`setup_name` via `Store.setup_names_for_trade_ids`, same resolution as the
weekly report) and per strategy.

Columns: `n`, `gross`, `fees@rate` and `net@rate` for each `--fee-rates` value
(defaults 0.40% / 0.60% / 0.90% per side), plus **actual** `fees` and `net`.
What-if nets reuse `trade_gross_pnl`, `trade_fee_legs_notional`,
`trade_net_at_fee_rate`, and `net_at_fee_rate`. Stored fills are not mutated.

**Break-even fee rate per side (All)** is the per-side rate `r` where
`gross - r * fee_legs_notional = 0`, i.e. `r = gross / fee_legs_notional`.
Printed as `n/a` when All gross P/L is <= 0.

## Retroactive I4 gates

Applies the I4 shadow-gate rules to the same closed cohort. Threshold is
`OpsConfig.shadow_gates.fee_hurdle_r_max` (default 0.5 R). Observational only.

- **fee-hurdle.** Uses stored `exit_snapshot.fee_hurdle_r` when present.
  Otherwise recomputes `fee_hurdle_r(entry_price, original_qty or qty,
  initial_risk_per_unit, fee_pct_per_side)`. `fee_pct_per_side` is snapshot
  `fee_hurdle_pct_per_side` when present, else that strategy's
  `assumed_fee_pct_per_side` from `get_strategies()` (read-only), else 0.006.
  Would-block when the hurdle is above the threshold. Coverage counts stored vs
  recomputed vs unavailable (`n` is the cohort size).
- **cross-sleeve.** Would-block if, at this trade's `opened_at`, another trade
  on the **same ticker** from a **different** strategy was open:
  `opened_at < this.opened_at` and (`closed_at is None` or
  `closed_at > this.opened_at`). Holders are **all** store trades (any
  fingerprint, open or closed).
- **union.** Would-block if either gate would.

Each gate row: blocked n / blocked net $ / blocked mean net R
(`trade_realized_r`), kept n / kept net $ / kept mean R, total.

## breakout_retest vs breakout_close by strategy

Rows are `(setup × strategy)` for `breakout_retest` and `breakout_close` only.
Columns: `n`, `gross`, `fees`, `net`, mean net R, net win %. Rows with
`n < 5` (`SMALL_N_THRESHOLD`) are tagged `[small-n]`.

# Shadow entry gates

Forward-only would-block flags recorded at entry. They do not change order
flow, sizing, entries, or exits.

## Snapshot keys

Written into `Trade.exit_snapshot` in `TradeManager.open_position` (version 1),
after `fee_hurdle_r` is computed. A recording failure logs a warning and the
entry continues.

- `shadow_gate_fee_hurdle_r_max` — threshold in R used for the fee-hurdle comparison
- `shadow_block_fee_hurdle` — true when `fee_hurdle_r` exceeds the threshold; null when the hurdle is unavailable
- `shadow_block_cross_sleeve` — true when another strategy already has an OPEN trade on the same ticker
- `shadow_cross_sleeve_holders` — sorted strategy names of those other holders
- `shadow_gates_version` — `1`

Threshold defaults (`fee_hurdle_r_max=0.5`, `enabled=true`) live on
`OpsConfig.shadow_gates` in code. They are not part of the hashed trading
policy and are not required in `config/ops.yaml`. `TradeManager` takes
`shadow_gate_fee_hurdle_r_max` (default 0.5) from `get_ops().shadow_gates`
where the manager is built.

## Weekly report

`build_weekly_report` adds a section:

`Shadow entry gates (forward-only, no behavior change; threshold X R)`

For the week's closed trades, and for closed trades whose
`config_fingerprint` starts with the gen-8 prefix (`c95a0ad410f4`): coverage
n (trades carrying the shadow keys), a `no shadow data` count, and per gate
(fee-hurdle, cross-sleeve, and the union `either`) flagged vs not-flagged
counts, net dollars, and mean net R.

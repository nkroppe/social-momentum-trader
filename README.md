# social-momentum-trader

A 24/7, **long-only spot** crypto trader driven primarily by multi-timeframe
**price action**, with tier-specific social catalysts, hard risk limits, and
layered fund-protection guardrails. Runs on a cloud VPS. Coinbase Advanced Trade
is the broker. **Paper mode is the default**.

> Not financial advice. Social momentum is noisy and most retail variants lose
> after fees/slippage. Trade only capital you can afford to lose.

## What it does

```
X recent counts (30m) -> anomaly-triggered 25-post samples -> quality metadata
   -> count-based attention z-score -> multi-timeframe price setup
   -> SHADOW tier social policy + SHADOW sparse L3 Sonnet review
   -> HARD RISK GATE (strategy + global portfolio heat/exposure)
   -> executable PAPER fill -> partial profit + protected Chandelier runner
```

- **Direction:** long-only spot (USD pairs on an allowlist).
- **Execution venue (locked):** [Coinbase Advanced Trade](docs/venue.md) — US spot only. No Robinhood, Phantom, or Bullpen in this repo.
- **Signals:** deterministic price/volume setups first. During the P0 shadow
  phase, social and LLM outcomes are audited but cannot approve, reject, boost,
  veto, or resize a paper candidate.

## How entries are gated

Every production entry starts with price. Intraday uses a 15-minute trigger and
1-hour bias; swing uses a 1-hour trigger and a deterministically aggregated
4-hour bias. Both require EMA 9/21/50 alignment, RSI(14) >= 55, N-bar structure,
and tier-relative volume. The completed setup must then pass the configured
SMA, trailing-return, and volume-z confirmation gates. Setups are
breakout-and-close or breakout-retest; majors and SOL may also use a
rolling-VWAP reclaim intraday. Swing rules are separate: compression is
required and VWAP pullbacks are disabled.

The tier playbook is evaluated only after the price setup. With
`social_decision_mode: shadow` (the shipped default), these are counterfactual
outcomes for audit and do not affect orders:

- **Major (BTC/ETH):** price-only; social is ignored.
- **Large (SOL):** price is hard; constructive social can boost conviction and
  sufficiently bearish social can veto.
- **Mid (HYPE/ZEC):** price setup plus required social confirmation.
- **Micro (PUMP/CAP):** social catalyst plus a hard price breakout/retest.

Social attention can never open a trade without a qualifying price trigger, and
in shadow mode it cannot close the gate or change size either.
Retests are preferred for majors/large and required for mid/micro. Relative
volume is at least 1.5x for major/large and 2.0x for mid/micro.
When position capacity is scarce, candidates are ranked only by deterministic
price evidence (setup quality, conviction, relative volume, ticker), never by
social z-score.

A benchmark regime filter (BTC vs its 50-day average) blocks *all* new entries
in a broad downtrend. Every price gate is **fail-closed**: no market data means
no entry.

### Sparse L3 Sonnet review

After deterministic gates pass, large/mid/micro candidates are queued for one
bounded Sonnet review through the Cursor SDK. BTC/ETH bypass the LLM. The review
records whether it would veto credible adverse events or require a stronger
catalyst. Calls are non-blocking and cached by setup, with a hard monthly
invocation cap. Pending, veto, score, and approval results do not block or resize
paper candidates while shadow mode is configured.

The Cursor agent runs with `tools=[]`: it cannot read files, run commands,
inspect VPS secrets, edit code, or place orders. It receives only bounded setup
metrics and recent posts. It does not label individual posts. Cursor SDK usage
uses the account's Cursor request pools/pricing and appears under the SDK tag in
the Cursor usage dashboard.

Each Sunday report also queues a Sonnet reflection over closed trades and
current rules. Recommendations are persisted and sent as an advisory Telegram
message; they are never applied automatically.

## Two strategies, one capital pool

The bot runs **two methodologies simultaneously** on a configurable capital
split (default 50/50) so you can compare which performs best over the soak:

Intraday targets 50% off at 1.5R with a 6-hour hard time-stop and a tighter
4-hour stale stop if price never reaches +1R. Swing targets 50% off at 2R with
48-hour/24-hour equivalents. After the partial, the remainder uses a
Chandelier ATR stop that only ratchets upward and cannot fall below a
cost-adjusted breakeven floor.

Position size starts from a 0.5% equity risk budget divided by the candidate's
structure-stop percentage. The result is capped by the hard max-position
percentage, liquidity tier, volatility, and setup conviction; no multiplier can
raise it above the hard cap. Daily and weekly halts include marked unrealized
P/L and fail conservatively when an open position cannot be quoted.
Before entry, the first partial must remain profitable after modeled fees,
spread, slippage, and visible-depth constraints. Across both strategies, global
limits cap open heat at 2%, gross exposure at 50%, combined exposure per ticker
at 10%, and aggregate micro exposure at 15%.

Each strategy sizes off its **own** allocation half and enforces its **own**
limits (max position %, max open, max trades/day, daily/weekly loss halts,
cooldown). One strategy hitting a limit or loss-halt does **not** affect the
other. A ticker may be held by both strategies independently; every trade is
tagged with the strategy that opened it, while the global limits still see both
positions. Enabled allocations must sum to <= 1.0.

### PAPER execution and research evidence

Deployed PAPER does not use synthetic or last-candle fallback prices. Entries
require a fresh, contiguous one-minute feed and a fresh Coinbase level-1 book.
Buys fill at ask plus adverse slippage; sells fill at bid minus adverse
slippage. Entries are rejected when spread exceeds 40 bps, visible top-level
depth is insufficient, or the order would consume more than 50% of that level.
Stops and targets walk each newly closed one-minute bar exactly once; a bar that
touches both is resolved stop-first. Synthetic prices remain explicit to
`smt simulate` and tests only.

Every closed trigger candle creates a versioned prospective opportunity record,
including rejected/no-setup outcomes. Candidate rows are enriched with
social/Sonnet, risk, execution, and trade linkage, then labeled prospectively
with 1h/4h/24h/72h return, MAE, and MFE when enough future candles exist.

For network-free price-only research, provide contiguous UTC OHLCV files named
`PRODUCT-ID.csv` with the exact header
`timestamp,open,high,low,close,volume`, then run:

```bash
smt backtest --data-dir data/candles --symbols BTC ETH SOL \
  --start 2025-01-01T00:00:00Z --end 2026-01-01T00:00:00Z \
  --output-dir data/backtest
```

Replay decisions occur after candle close and fills occur no earlier than the
next bar. Artifacts include the policy/data manifest, opportunities, trades,
equity curve, after-cost metrics, and simple price baselines. This command does
not backtest social or Sonnet because complete point-in-time histories do not
exist.

## Quick start (paper, no credentials needed)

```bash
# 1) Install (dev tools + core deps)
python -m venv .venv && . .venv/Scripts/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 2) Prove the whole pipeline (BOTH strategies) with a deterministic demo
smt simulate --ticker SOL

# 3) Compare strategy performance side by side
smt compare

# 4) See current momentum scores (uses mock data until real sources are set)
smt score

# 5) Run the loop in paper mode
smt run
```

With no `.env`, it uses **SQLite** and a **mock** social feed, so the loop and
the `simulate` demo run with zero external accounts.

## Configuration

- `config/risk.yaml` - global hard caps / shared defaults (inherited by strategies)
- `config/strategies.yaml` - per-strategy enabled flag, allocation, exit params, and signal thresholds; any omitted field inherits from `risk.yaml`
- `config/universe.yaml` - tradeable USD spot pairs + mention aliases
- `config/sources.yaml` - Reddit/X polling + mock toggle
- `config/security.yaml` - fund-protection controls
- `config/ops.yaml` - soak tracking, digest interval, preflight requirements
- `config/market.yaml` - price confirmation, regime filter, volatility sizing
- `config/signals.yaml` - spam filters, sentiment lexicon, per-tier signal profiles
- `config/llm.yaml` - sparse Sonnet judge, call budget, cache, weekly reflection
- `.env` (copy from `.env.example`) - secrets + mode flags
- `.env.production.example` - VPS production template (Postgres, alerts, no mock)

### X (Twitter) production setup

1. Create an X developer app and generate a **Bearer Token** (pay-per-use API).
2. Set `X_BEARER_TOKEN`. The default shared ceiling is
   `X_MONTHLY_BUDGET_USD=100`; both endpoint prices are configurable because X
   pricing can change.
3. `config/sources.yaml` requests uncensored recent counts for each cashtag in
   UTC-aligned 30-minute windows, then samples 25 posts only on adaptive
   anomalies or scheduled cold-start windows. Watch accounts remain trusted
   sampled event feeds.
4. Distinct post reads, count requests, endpoint spend, and daily/monthly dollar
   pace are tracked atomically in `data/x_budget.json`.
5. On the VPS, set `mock.enabled: false` once Reddit + X credentials are configured.

### Cursor LLM setup

1. Create a user API key in **Cursor Dashboard → Integrations**.
2. Set `CURSOR_API_KEY` in the VPS `.env`.
3. Rebuild the image; Docker installs the `llm` extra automatically.
4. Run `docker compose exec trader smt doctor`. The `cursor_llm` check must pass.

The exact Sonnet model ID is discovered from the API key's current model catalog,
so the code does not hard-code a stale model version.

### Schema migrations

`init_db()` runs lightweight, idempotent SQLite/Postgres migrations. Social
counts, richer X author/engagement metadata, and stable shadow-decision audit
records are persisted alongside the prospective opportunity ledger and existing
trade migration fields.
Existing open trades are conservatively backfilled from their stored entry and
stop. No manual migration step is required.

## Going live on Coinbase Advanced (only after a clean paper soak)

Execution is **Coinbase Advanced Trade only** — see [docs/venue.md](docs/venue.md).

> Deployment warning: the shipped social layer is intentionally `shadow`.
> Do not change it to `enforce` or treat shadow audit results as live-ready
> evidence without a separately reviewed paper-validation period.

Two independent latches must both be set, and the Coinbase key must pass the
trade-only assertion:

1. `LIVE=true`
2. `LIVE_ACK=I_UNDERSTAND_LIVE_RISK`
3. Coinbase API key = **View + Trade only** (Transfer disabled). On startup the
   app calls `get_api_key_permissions()` and **refuses to run** if
   `can_transfer` is true.

Advanced partial/Chandelier management is currently **PAPER-only**. `smt doctor
--live` and Runner startup explicitly block LIVE while it is enabled because
safe Coinbase server-side bracket adjustment parity is not implemented.

See `docs/compromise-runbook.md` and the fund-protection layers below.

## Fund protection (if API keys are stolen)

1. **Trade-only key** - Transfer permission never enabled; startup assertion.
2. **IP allowlist** - key bound to the VPS static IP.
3. **Capital isolation** - dedicated small bot portfolio; main funds elsewhere.
4. **Account hardening** - withdrawal address allowlist + hardware 2FA.
5. **Code deny-list** - executor blocks any transfer/withdraw/convert path.
6. **Monitoring + kill** - transfer/anomaly alerts; `touch control/KILL`.
7. **VPS hygiene** - non-root, SSH keys only, secrets not in git, rotation.

## Deploy on the VPS

See [docs/deploy-vps.md](docs/deploy-vps.md) for the full guide. Quick path:

```bash
sudo bash scripts/vps-setup.sh          # once, as root
cp .env.production.example .env         # fill Reddit, X, Postgres, alerts
# set mock.enabled: false in config/sources.yaml
docker compose up -d --build
docker compose exec trader smt doctor     # verify VPS paper config
docker compose logs -f trader
```

During the paper soak, the bot tracks elapsed days in `data/soak.json`, sends
daily digest alerts, and blocks live mode until the minimum soak is met. The
clock is bound to a canonical SHA-256 of the resolved trading policy. A relevant
strategy, risk, market, signal, universe, source, or LLM policy change starts a
new generation automatically while retaining bounded prior-generation history.

## Ops commands

```bash
smt doctor              # production preflight (VPS paper profile)
smt doctor --dev        # minimal config checks for local dev
smt doctor --live       # go-live checks (Coinbase key, soak duration, LIVE flags)
smt test-alerts         # send a test alert to configured channels
smt soak-report         # soak progress + strategy comparison
smt preview             # live exit levels + position size per symbol
smt weekly-report       # preview this week's P/L (--send to deliver, --last for prior week)
smt shadow-report       # social + Sonnet readiness evidence (--days N, --send)
smt backtest ...        # deterministic local price-only replay; never calls network
smt soak-reset          # intentional restart; policy changes reset automatically
```

`smt soak-report` shows the active policy fingerprint, generation, reset reason,
and changed policy sections. Days accumulated under different rules do not
evidence the system that would go live; legacy or mismatched state fails closed.

### Interpreting `shadow-report`

`smt shadow-report` is read-only: it uses persisted count coverage, setup audits,
and exactly linked paper trades without ingesting, calling Sonnet, or placing an
order. It reports separate `SOCIAL` and `SONNET L3` verdicts plus per-tier
readiness, false-rejection outcomes, and net-R separation. `NOT READY` is the
expected result until the conservative sample, coverage, and observation floors
in `config/ops.yaml` are met. Outcome-group floors count only linked, closed
paper trades—not unresolved audit rows. Sonnet also requires at least 95%
completion across eligible pending/complete/error reviews and a low error rate.
Current X/count collection and the Sonnet judge must remain enabled; historical
rows cannot make a disabled evidence pipeline READY.

Activation is staged, never automatic: review the report, activate only an
individual READY tier in a separately approved paper rollout, re-observe it,
then consider another tier or layer. A social READY verdict does not authorize
Sonnet, and a Sonnet READY verdict does not authorize social enforcement. The
command never edits configuration or changes `social_decision_mode`.

### Notifications

Every entry and exit pushes a message, with realized P/L (dollars and percent),
exit reason, fees, and hold time on each sell. A performance report covering the
week's completed trades is sent on a schedule — by default Sunday 8 PM
`America/New_York`, configured in `config/ops.yaml`.

The schedule tracks local wall-clock time, so the hour holds across daylight
saving instead of drifting. The last send is persisted, so a restart neither
double-sends nor skips a week, and a report missed during downtime is delivered
late rather than dropped.

Email and Telegram receive everything. ntfy stays reserved for critical events
(kill switch, loss halts) so its urgent-priority push keeps its meaning.

Before enabling live trading, complete [docs/go-live-checklist.md](docs/go-live-checklist.md)
and run `smt doctor --live` until all checks pass.

## Kill switch

```bash
touch control/KILL      # stop entries + flatten positions
smt kill --reason "..." # same, via CLI
smt clear-kill          # resume
```

## Safety / status

```bash
smt status              # open positions + allocation equity, by strategy
smt compare             # per-strategy trades, win rate, PnL, avg hold
```

## Layout

```
src/smt/
  ingest/   reddit, x, mock + ticker extraction, spam/sentiment quality filter
  scorer/   mention-velocity z-score with a seasonal baseline
  market/   Coinbase candles, ATR/SMA/volume indicators, regime filter
  trader/   signals (per-strategy), risk gate (per-strategy), paper + coinbase brokers, trade manager
  ops/      alerts, kill switch, soak tracker, preflight (doctor)
  demo.py   deterministic seeding for simulate/tests
  run.py    orchestrator     cli.py  CLI
config/     risk, strategies, universe, sources, security, ops, market, signals
docs/       venue.md, deploy-vps.md, go-live-checklist.md, compromise-runbook.md
```

## Roadmap

1. **Now:** prove social-momentum on Coinbase spot (intraday + swing, paper then live).
2. **Later (separate repo):** Solana meme-coin bot on-chain — different venue, wallet, and risk model; not mixed with this stack.

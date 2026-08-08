# social-momentum-trader

A 24/7, **long-only spot** crypto trader driven by **social-momentum** signals
(Reddit + X; mock fallback for local dev), with **hard risk limits** and layered
**fund-protection guardrails**. Runs on a cloud VPS. Coinbase Advanced Trade is
the broker. **Paper mode is the default**; live trading is gated behind explicit
safety latches and a paper soak.

> Not financial advice. Social momentum is noisy and most retail variants lose
> after fees/slippage. Trade only capital you can afford to lose.

## What it does

```
ingest (Reddit/X/mock) -> normalize/dedupe -> velocity z-score
   -> per-strategy signal (multi-source confirmation) -> HARD RISK GATE (per strategy)
   -> paper/live executor (entry + TP/SL + time-stop) -> manage exits
```

- **Direction:** long-only spot (USD pairs on an allowlist).
- **Execution venue (locked):** [Coinbase Advanced Trade](docs/venue.md) — US spot only. No Robinhood, Phantom, or Bullpen in this repo.
- **Signals v1:** keyword + mention-velocity only. No LLM (deferred to phase 2).

## Two strategies, one capital pool

The bot runs **two methodologies simultaneously** on a configurable capital
split (default 50/50) so you can compare which performs best over the soak:

| Strategy | Hold | Take-profit | Stop-loss | Time-stop | Entry thresholds |
|---|---|---|---|---|---|
| `intraday` | hours-intraday | +6% | -3% | 6h | z>=2.5, >=2 sources, >=8 mentions, 30m x 8 buckets |
| `swing` | 1-3 days | +15% | -7% | 48h (max 72h) | z>=3.0, >=2 sources, >=15 mentions, 120m x 12 buckets |

Each strategy sizes off its **own** allocation half and enforces its **own**
limits (max position %, max open, max trades/day, daily/weekly loss halts,
cooldown). One strategy hitting a limit or loss-halt does **not** affect the
other. A ticker may be held by both strategies independently; every trade is
tagged with the strategy that opened it. Enabled allocations must sum to <= 1.0.

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
- `.env` (copy from `.env.example`) - secrets + mode flags

### X (Twitter) production setup

1. Create an X developer app and generate a **Bearer Token** (pay-per-use API).
2. Set `X_BEARER_TOKEN` and optionally `X_MONTHLY_READ_BUDGET` (default 50,000 reads/mo) in `.env`.
3. `config/sources.yaml` has `x.enabled: true` with cashtag watchlist queries for the universe.
4. Reads are tracked in `data/x_budget.json`; polling stops when the monthly cap is hit.
5. On the VPS, set `mock.enabled: false` once Reddit + X credentials are configured.

### Schema migrations

`init_db()` runs a lightweight, idempotent migration that adds the
`trades.strategy` column to databases created before dual-strategy support
(SQLite `ALTER TABLE ... ADD COLUMN`; Postgres `ADD COLUMN IF NOT EXISTS`).
No manual step is required; existing rows default to `intraday`. To start
fresh in dev instead, delete `data/smt.sqlite`.

## Going live on Coinbase Advanced (only after a clean paper soak)

Execution is **Coinbase Advanced Trade only** — see [docs/venue.md](docs/venue.md).

Two independent latches must both be set, and the Coinbase key must pass the
trade-only assertion:

1. `LIVE=true`
2. `LIVE_ACK=I_UNDERSTAND_LIVE_RISK`
3. Coinbase API key = **View + Trade only** (Transfer disabled). On startup the
   app calls `get_api_key_permissions()` and **refuses to run** if
   `can_transfer` is true.

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

```bash
cp .env.example .env    # fill in secrets; set DATABASE_URL to Postgres
docker compose up -d --build
docker compose logs -f trader
```

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
  ingest/   reddit, x, mock + ticker extraction
  scorer/   mention-velocity z-score
  trader/   signals (per-strategy), risk gate (per-strategy), paper + coinbase brokers, trade manager
  ops/      alerts, kill switch
  demo.py   deterministic seeding for simulate/tests
  run.py    orchestrator     cli.py  CLI
config/     risk, strategies, universe, sources, security
docs/       venue.md, compromise-runbook.md
```

## Roadmap

1. **Now:** prove social-momentum on Coinbase spot (intraday + swing, paper then live).
2. **Later (separate repo):** Solana meme-coin bot on-chain — different venue, wallet, and risk model; not mixed with this stack.

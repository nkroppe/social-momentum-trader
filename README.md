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
ingest (Reddit/X/mock) -> spam filter + sentiment -> velocity z-score (seasonal baseline)
   -> per-strategy signal (tiered social + price confirmation) -> HARD RISK GATE (per strategy)
   -> paper/live executor (ATR entry + TP/SL + time-stop) -> manage exits
```

- **Direction:** long-only spot (USD pairs on an allowlist).
- **Execution venue (locked):** [Coinbase Advanced Trade](docs/venue.md) — US spot only. No Robinhood, Phantom, or Bullpen in this repo.
- **Signals:** keyword velocity, lexicon sentiment, and price/volume confirmation. No LLM (deferred to phase 2).

## How entries are gated

Social attention alone is a weak signal, and it gets weaker as market cap rises:
published studies find crowd trading signals roughly twice as predictive for
low-cap coins as for high-cap ones, and attention spikes on majors are often
*contrarian*. So each symbol carries a `tier` in `config/universe.yaml` that
selects which gates must pass:

| Tier | Signal mode | Social gate | Trend gate | Direction gate |
|---|---|---|---|---|
| `major` (BTC, ETH) | `trend` | ignored | required | required |
| `large` / `mid` | `hybrid` | required | required | required |
| `micro` (PUMP) | `social` | required | ignored | required |

- **Social gate** — velocity z-score, raw mentions, distinct *accounts*, and a
  bullish-vs-bearish ratio. Distinct authors matter most when running a single
  source: it stops one loud account manufacturing a signal.
- **Trend gate** — price above its moving average with volume participation.
- **Direction gate** — positive trailing return over the strategy's window. This
  always applies. Buying an attention spike caused by a crash is the most
  damaging failure mode of a pure mention count.

A benchmark regime filter (BTC vs its 50-day average) blocks *all* new entries
in a broad downtrend. Every price gate is **fail-closed**: no market data means
no entry.

## Two strategies, one capital pool

The bot runs **two methodologies simultaneously** on a configurable capital
split (default 50/50) so you can compare which performs best over the soak:

| Strategy | Hold | Take-profit | Stop-loss | Time-stop | Entry thresholds |
|---|---|---|---|---|---|
| `intraday` | hours-intraday | 2x ATR | 1x ATR | 6h | z>=2.5, >=3 authors, >=8 mentions, >=60% bullish, 30m x 8 buckets |
| `swing` | 1-3 days | 2x ATR | 1x ATR | 48h (max 72h) | z>=3.0, >=5 authors, >=15 mentions, >=65% bullish, 120m x 12 buckets |

Exits are **volatility-scaled**, not fixed percentages. ATR is scaled to each
strategy's holding period (volatility grows with the square root of time), so
one rule fits a universe spanning BTC and a sub-cent token. A fixed +6%/-3%
makes BTC targets unreachable within 6 hours — nearly every trade would end on
the time stop at a random price — while sitting inside a micro cap's noise band.
Run `smt preview` to see the live levels per symbol. Position size is scaled the
same way, so a high-volatility asset risks the same dollars as a calm one.

Take-profit is never allowed inside round-trip fees.

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
- `config/ops.yaml` - soak tracking, digest interval, preflight requirements
- `config/market.yaml` - price confirmation, regime filter, volatility sizing
- `config/signals.yaml` - spam filters, sentiment lexicon, per-tier signal profiles
- `.env` (copy from `.env.example`) - secrets + mode flags
- `.env.production.example` - VPS production template (Postgres, alerts, no mock)

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
daily digest alerts, and blocks live mode until the minimum soak is met.

## Ops commands

```bash
smt doctor              # production preflight (VPS paper profile)
smt doctor --dev        # minimal config checks for local dev
smt doctor --live       # go-live checks (Coinbase key, soak duration, LIVE flags)
smt test-alerts         # send a test alert to configured channels
smt soak-report         # soak progress + strategy comparison
smt preview             # live exit levels + position size per symbol
smt soak-reset          # restart the soak clock after changing signal logic
```

Reset the soak clock whenever entry or exit logic changes. Days accumulated
under different rules do not evidence the system that would go live.

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

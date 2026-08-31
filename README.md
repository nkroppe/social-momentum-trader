# social-momentum-trader

A 24/7, **long-only spot** crypto trader driven by multi-timeframe **price
action**, hard risk limits, and fund-protection guardrails. Coinbase Advanced
Trade is the only broker. **Paper mode is the default** and is what the VPS
runs.

> Not financial advice. Most retail variants lose after fees and slippage.
> Trade only capital you can afford to lose.

## What is live on the VPS (as of 2026-08-27)

The production paper box tracks **`main`**.

| Piece | Live setting |
| --- | --- |
| Mode | PAPER, Coinbase Advanced marks, Postgres |
| Paper equity | $10,000 starting |
| Allocations | **20% intraday / 60% swing / 20% bear_rally** |
| Social ingest | **Off.** `sources.x.enabled: false`, Reddit unapproved, mock disabled. Price-only loop. |
| Intraday entries | 15m trigger / 1h bias. **Breakout-close and VWAP only** — `breakout_retest` disabled. Min stop **2%** so 1R is larger than ~1.2% round-trip fees. |
| Swing entries | 1h trigger / 4h bias. Close or retest. VWAP off. Min stop 1%. |
| Universe | BTC, ETH, SOL, HYPE, ZEC, PUMP. Mid/micro require retest, so HYPE/ZEC/PUMP do **not** open on the intraday sleeve. |
| Exits | Intraday `intraday_trend_v2`: 25% at 1.5R, Chandelier 2.5 ATR on 1h bars, 12h time / 4h stale if MFE < 0.5R. Swing `swing_trend_v2`: 25% at 2.0R, Chandelier 3.0 ATR on 4h bars, **120h** time / 24h stale. bear_rally `bear_reversion_v2`: 50% at 1.0R, 6h / 2h, RISK-OFF only. |
| Fees in paper | `assumed_fee_pct_per_side: 0.006` |
| Soak | Policy-bound generations in `data/soak.json`. Gen **8** started 2026-08-27 13:34 UTC after the allocation/retest/stop-floor change. Changing strategies/risk/market/signals/universe/llm starts a new generation. A **sources-only** change (X/Reddit on/off) updates the fingerprint in place and **does not** reset the clock. |

Social and Sonnet code is still in the repo and stays in **shadow** (audit only).
With X paused those paths are a no-op on the live book.

## What it does

```
Coinbase candles + L1 book
   -> multi-timeframe price setup (per strategy)
   -> HARD RISK GATE (strategy + global heat/exposure)
   -> executable PAPER fill (ask/bid, spread, depth)
   -> 25% partial + Chandelier runner (PAPER-only advanced exits)
```

- **Direction:** long-only spot (USD pairs on an allowlist).
- **Execution venue (locked):** [Coinbase Advanced Trade](docs/venue.md) — US spot only. No Robinhood, Phantom, or Bullpen in this repo.
- **Signals on VPS:** deterministic price/volume only. Social ingest is paused.
  If X is turned back on, keep `social_decision_mode: shadow` until a separate
  review; shadow outcomes still cannot approve, reject, boost, veto, or resize.

## How entries are gated

Every production entry starts with price. Intraday uses a 15-minute trigger and
1-hour bias; swing uses a 1-hour trigger and a deterministically aggregated
4-hour bias. Both require trigger EMA 9/21/50 alignment, N-bar structure, and
tier-relative volume. Intraday also requires RSI(14) >= 55 and a bullish 1h
EMA stack. Swing uses RSI(14) >= 50 and only rejects a bearish 4h stack (it
does not require a full 9>21>50 bias in a grind). The completed setup must
then pass the configured SMA, trailing-return, and volume-z confirmation
gates.

**Intraday (live):** breakout-and-close, plus rolling-VWAP reclaim on
majors/large. `allow_breakout_retest: false` after the Aug 24–27 chop (retest
was 0/5, −$47). Structure stops are floored at **2%**. Mid/micro tiers still
*require* retest in `signals.yaml`, so HYPE/ZEC/PUMP will not receive new
intraday entries until that policy changes.

**Swing (live):** breakout-and-close or breakout-retest. VWAP pullbacks off.
Compression is a ranking preference, not a hard gate.

The tier playbook is evaluated only after the price setup. With
`social_decision_mode: shadow` (the shipped default), these are counterfactual
outcomes for audit and do not affect orders:

- **Major (BTC/ETH):** price-only; social is ignored.
- **Large (SOL):** price is hard; constructive social can boost conviction and
  sufficiently bearish social can veto.
- **Mid (HYPE/ZEC):** price setup plus required social confirmation.
- **Micro (PUMP):** social catalyst plus a hard price breakout/retest.

Social attention can never open a trade without a qualifying price trigger, and
in shadow mode it cannot close the gate or change size either. On the live VPS
X/Reddit collectors are disabled, so these tier social rules do not run.
Retests remain preferred for majors/large and required for mid/micro **on
swing**. Relative volume is at least 1.5x for major/large and 2.0x for mid/micro.
When position capacity is scarce, candidates are ranked only by deterministic
price evidence (setup quality, conviction, relative volume, ticker), never by
social z-score.

A benchmark regime filter (BTC vs its 50-day average) gates entries
**per strategy**: `intraday` / `swing` only enter in RISK-ON; `bear_rally`
only enters in RISK-OFF. RISK-ON also requires BTC 4h not printing consecutive
lower lows. Setup relative volume is the volume gate; the extra 1h volume-z
confirmation is off. Every price gate is
**fail-closed**: no market data means no entry.

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

## Strategies, one capital pool

The bot runs **three methodologies** on a configurable capital split. **Live
VPS is 20/60/20** (intraday / swing / bear_rally). Bull strategies only take new
entries when BTC is above its 50-day SMA; bear_rally only when BTC is below it
(so idle capital sits in cash in the "wrong" regime). RISK-ON also requires BTC
4h not printing consecutive lower lows.

Intraday (`intraday_trend_v2`) takes **25%** off at **1.5R**, then a Chandelier
at 2.5 ATR on 1h bars; hard time-stop **12h**, stale **4h** if MFE never
reaches 0.5R. Swing (`swing_trend_v2`) takes **25%** off at **2.0R**,
Chandelier at 3.0 ATR on 4h bars; hard time-stop **120h**, stale **24h**. `bear_rally` (`bear_reversion_v2`) trades
short-lived RISK-OFF relief rallies on BTC/ETH/SOL (RSI reclaim, failed
breakdown, relative-strength bounce) with **50%** off at **1.0R**, 6h / 2h
stops. After the partial, the remainder uses a Chandelier ATR stop that only
ratchets upward and cannot fall below a cost-adjusted breakeven floor. Advanced
partial/Chandelier management is **PAPER-only**.

Position size starts from a 0.5% equity risk budget divided by the candidate's
structure-stop percentage. The result is capped by the hard max-position
percentage, liquidity tier, volatility, and setup conviction; no multiplier can
raise it above the hard cap. Daily and weekly halts include marked unrealized
P/L and fail conservatively when an open position cannot be quoted.
Before entry, the first partial must remain profitable after modeled fees,
spread, slippage, and visible-depth constraints. Across both strategies, global
limits cap open heat at 2%, gross exposure at 50%, combined exposure per ticker
at 10%, and aggregate micro exposure at 15%.

Each strategy sizes off its **own** allocation slice and enforces its **own**
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

# 6) Optional: monitoring dashboard (read-only)
pip install -e ".[dashboard]"
smt dashboard
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

### X (Twitter) — paused on the live VPS

X ingest is **off** (`sources.x.enabled: false` and `ops.preflight.require_x: false`).
Empty social collectors do **not** fall back to mock. Doctor reports `x_ingest:
paused (price-only)`. Flipping X back on requires setting **both** `enabled` and
`require_x` together. That hashes `sources` but does **not** start a new soak
generation (sources is a keep-in-place fingerprint section).

The collector code still exists: Bearer Token, `$100/month` ceiling, 30-minute
cashtag counts, anomaly-triggered 25-post samples, `data/x_budget.json`. It is
not in the live path until those flags change.

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

> Deployment warning: VPS paper is **price-only**. Social is paused, and the
> remaining social/LLM layer is `shadow` (audit only). Do not set
> `social_decision_mode: enforce` or treat shadow rows as live-ready evidence
> without a separately reviewed paper period with ingest actually on.

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
# VPS tracks main
cp .env.production.example .env         # Postgres, alerts, Cursor key; X/Reddit optional
# mock.enabled: false; sources.x.enabled: false on the live box
docker compose up -d --build
docker compose exec trader smt doctor     # verify VPS paper config
docker compose logs -f trader
```

During the paper soak, the bot tracks elapsed days in `data/soak.json`, sends
daily digest alerts, and blocks live mode until the minimum soak is met. The
clock is bound to a canonical SHA-256 of the resolved trading policy. A
strategies, risk, market, signals, universe, or LLM change starts a new
generation automatically (bounded prior-generation history is kept). Turning
X/Reddit ingest on or off is a sources-only identity update and keeps the
current generation.

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
smt dashboard           # read-only web UI on http://127.0.0.1:8080
smt backtest ...        # deterministic local price-only replay; never calls network
smt soak-reset          # intentional restart; policy changes reset automatically
```

### Monitoring dashboard

A FastAPI + React dashboard reports open positions, trade history, equity, P&L,
strategy comparison, opportunity funnel, and risk caps. It is **read-only**
(no orders, no kill switch).

Local (loopback, token optional):

```bash
pip install -e ".[dashboard]"
cd web && npm install && npm run build && cd ..
smt dashboard
```

Or keep the API on 8080 and the Vite dev server on 5173 (`npm run dev` in `web/`).

On the VPS, Compose publishes **loopback only**: `127.0.0.1:8080`. Set a long
`DASHBOARD_TOKEN` in `.env`. Reach it with an SSH tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 user@your-vps
# then open http://127.0.0.1:8080 and paste the token
```

Do not publish port 8080 on `0.0.0.0`.

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
With X ingest paused, `shadow-report` cannot become READY: historical rows
cannot make a disabled evidence pipeline READY. Turn ingest back on only as a
deliberate soak-generation change, then collect a new sample.

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
late rather than dropped. `weekly_report.extra_email_to` in `config/ops.yaml`
adds SMTP copies of that Sunday digest and the LLM reflection (not trade
alerts) to listed addresses such as the AgentMail inbox.

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

From the configured Telegram chat (`TELEGRAM_CHAT_ID`), send an exact message:

- `KILL` — flatten every open position and pause new entries
- `START` — clear the kill switch and resume trading

Only those exact words (optional surrounding whitespace) from the authorized chat
are accepted. The bot acknowledges each command with an alert.

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

1. **Now:** finish the gen-8 paper soak on Coinbase spot (swing-heavy, price-only,
   no hashed YAML churn). Social stays parked.
2. **Later (separate repo):** Solana meme-coin bot on-chain — different venue, wallet, and risk model; not mixed with this stack.

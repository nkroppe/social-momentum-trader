# social-momentum-trader

A 24/7, **long-only spot** crypto trader driven by **social-momentum** signals
(Reddit + YouTube now; X deferred), with **hard risk limits** and layered
**fund-protection guardrails**. Runs on a cloud VPS. Coinbase Advanced Trade is
the broker. **Paper mode is the default**; live trading is gated behind explicit
safety latches and a paper soak.

> Not financial advice. Social momentum is noisy and most retail variants lose
> after fees/slippage. Trade only capital you can afford to lose.

## What it does

```
ingest (Reddit/YouTube/mock) -> normalize/dedupe -> velocity z-score
   -> signal (multi-source confirmation) -> HARD RISK GATE
   -> paper/live executor (entry + TP/SL + time-stop) -> manage exits
```

- **Direction:** long-only spot (USD pairs on an allowlist).
- **Hold horizon:** hours to intraday (time-stop flattens stragglers).
- **Signals v1:** keyword + mention-velocity only. No LLM (deferred to phase 2).
- **Region/broker:** US -> Coinbase Advanced Trade spot.

## Quick start (paper, no credentials needed)

```bash
# 1) Install (dev tools + core deps)
python -m venv .venv && . .venv/Scripts/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 2) Prove the whole pipeline end-to-end with a deterministic demo
smt simulate --ticker SOL

# 3) See current momentum scores (uses mock data until real sources are set)
smt score

# 4) Run the loop in paper mode
smt run
```

With no `.env`, it uses **SQLite** and a **mock** social feed, so the loop and
the `simulate` demo run with zero external accounts.

## Configuration

- `config/risk.yaml` - hard limits, exit params, signal thresholds
- `config/universe.yaml` - tradeable USD spot pairs + mention aliases
- `config/sources.yaml` - Reddit/YouTube/X polling + mock toggle
- `config/security.yaml` - fund-protection controls
- `.env` (copy from `.env.example`) - secrets + mode flags

## Going live (only after a clean paper soak)

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
smt status              # open positions + realized PnL
```

## Layout

```
src/smt/
  ingest/   reddit, youtube, x (stub), mock + ticker extraction
  scorer/   mention-velocity z-score
  trader/   signals, risk gate, paper + coinbase brokers, trade manager
  ops/      alerts, kill switch
  run.py    orchestrator     cli.py  CLI
config/     risk, universe, sources, security
docs/       compromise-runbook.md
```

# Execution venue (locked)

This bot trades **only** on **Coinbase Advanced Trade** (US spot, USD pairs).

## Why Coinbase Advanced

- **Long-only spot** on liquid majors (BTC, ETH, SOL, …) via `config/universe.yaml`
- **Attached TP/SL** on entry (`trigger_bracket_gtc`) — server-side exits in live mode
- **Trade-only API keys** — View + Trade, Transfer disabled; startup asserts `can_transfer=false`
- **Isolated portfolio** — bot capital in a dedicated Coinbase portfolio, separate from savings
- **Deterministic REST API** — Python service on a VPS, not wallet signing or agent CLI

Robinhood, Phantom, and Bullpen are **out of scope** for this repo. They target
different products (broker crypto, self-custody swaps, perps/on-chain) and do not
match the custodial guardrail model here.

## Live setup checklist

1. Coinbase Advanced account (US).
2. Create an **isolated portfolio** for bot capital only.
3. CDP API key scoped to that portfolio: **View + Trade**, **Transfer off**.
4. IP allowlist the key to the VPS static IP.
5. Account-level withdrawal address allowlist + hardware 2FA.
6. Set `COINBASE_API_KEY`, `COINBASE_API_SECRET`, `COINBASE_PORTFOLIO_ID` in `.env`.
7. Paper soak complete; then `LIVE=true` and `LIVE_ACK=I_UNDERSTAND_LIVE_RISK`.

See also `docs/compromise-runbook.md` and `config/security.yaml`.

## Future: separate Solana meme-coin bot

After this model is proven on Coinbase spot, a **separate project** will trade
Solana meme coins **on-chain** (Jupiter/DEX, wallet signing, different risk model).
That bot will **not** share capital, code, or credentials with this repo.

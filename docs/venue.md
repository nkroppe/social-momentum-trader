# Execution venue (locked)

This bot trades **only** on **Coinbase Advanced Trade** (US spot, USD pairs).

## Why Coinbase Advanced

- **Long-only spot** on liquid majors (BTC, ETH, SOL, …) via `config/universe.yaml`
- **Live order flow** (fixed in code, not live-verified): market buy
  (`market_order_buy` with `quote_size`), then a reduce-only sell bracket
  (`trigger_bracket_order_gtc_sell` with filled `base_size`, TP/SL). After a
  partial, protection is a stop-only `stop_limit_order_gtc_sell` on the
  chandelier trail — matching paper, which never re-checks take-profit.
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

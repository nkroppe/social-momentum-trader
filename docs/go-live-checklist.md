# Go-live checklist (Coinbase Advanced)

Complete **after** a successful 2-week paper soak on the VPS. Every item must be checked before setting `LIVE=true`.

## Paper soak complete

- [ ] `smt soak-report` shows ≥ 14 days elapsed (or your configured minimum)
- [ ] `smt compare` reviewed; intraday vs swing behavior understood
- [ ] No unexplained errors in `docker compose logs trader` over the soak period
- [ ] Kill switch tested once (`touch control/KILL`, verify flatten, `smt clear-kill`)

## Coinbase account

- [ ] Coinbase Advanced account (US) verified
- [ ] **Isolated portfolio** created for bot capital only
- [ ] Main savings in a **separate portfolio** the bot key cannot access
- [ ] Withdrawal **address allowlist** configured (or empty = no destinations)
- [ ] Hardware 2FA enabled on the Coinbase account

## API key (trade-only)

- [ ] CDP API key created with **View + Trade** only
- [ ] **Transfer permission disabled** (`can_transfer=false`)
- [ ] Key scoped to the **bot portfolio** only
- [ ] **IP allowlist** set to VPS static public IP
- [ ] Key nickname documented (e.g. `momentum-bot-trade-only`)

## Environment

- [ ] `.env` has `COINBASE_API_KEY`, `COINBASE_API_SECRET`, `COINBASE_PORTFOLIO_ID`
- [ ] `LIVE=true` and `LIVE_ACK=I_UNDERSTAND_LIVE_RISK`
- [ ] `mock.enabled: false` in `config/sources.yaml`
- [ ] Reddit + X credentials still valid
- [ ] At least one alert channel configured (email or ntfy/Telegram)

## Automated verification

```bash
docker compose exec trader smt doctor --live
```

All checks must pass. If any fail, **do not** restart with live mode.

## First live session

- [ ] Start with **minimal capital** ($500–1,000) in the isolated portfolio
- [ ] Watch first 24h manually via `smt status` and Coinbase dashboard
- [ ] Confirm bracket TP/SL appear on Coinbase after each entry
- [ ] Keep kill switch path ready (`control/KILL`)

## If something goes wrong

See [compromise-runbook.md](compromise-runbook.md): revoke API key from a trusted device immediately.

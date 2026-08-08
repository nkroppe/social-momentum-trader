# Compromise runbook

If you suspect the VPS or the Coinbase API key is compromised, act from a
**trusted device** (your phone/laptop), NOT the possibly-compromised host.

## Immediate (minutes matter)

1. **Revoke the API key** in the Coinbase dashboard (Settings -> API). This
   instantly stops all bot trading.
2. **Trip the kill switch** if the host is still reachable:
   - `touch control/KILL` (host) or `smt kill --reason compromise`.
   - Or `docker compose down` to stop containers.
3. **Confirm no withdrawals occurred.** The key is trade-only (`can_transfer`
   should be false), so API withdrawals should be impossible. Verify anyway in
   the Coinbase activity log.

## Contain

4. Rotate every other secret that lived on the host: Reddit/X tokens,
   SMTP/Telegram credentials, `POSTGRES_PASSWORD`.
5. Rebuild the VPS from a clean image. Do not trust a compromised host.
6. Re-provision with a fresh trade-only key + IP allowlist bound to the new IP.

## Verify guardrails held

7. `can_transfer` was false the whole time (startup assertion logs +
   `security_events` table `kind=startup`).
8. Withdrawal address allowlist on the account contained only your cold wallet
   (or was empty). No new addresses were added.
9. Isolated portfolio balance only; main funds untouched in a separate account.

## Residual risk reminder

A trade-only key cannot withdraw, but a thief with the key could place losing
trades using the isolated balance until the key is revoked. That is why the
isolated portfolio is kept small and daily/weekly loss halts are enabled.

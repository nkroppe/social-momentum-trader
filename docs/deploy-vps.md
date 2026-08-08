# VPS deployment guide

Deploy the trader on a **2–4 GB Ubuntu VPS** (DigitalOcean, Hetzner, Vultr, etc.) for the 2-week paper soak, then optional live trading on Coinbase Advanced.

## 1. Create the VPS

- **OS:** Ubuntu 22.04 or 24.04 LTS
- **Size:** 2 GB RAM minimum (4 GB recommended with Postgres)
- **Region:** US or close to you (latency to Coinbase is not critical)
- **Auth:** SSH key only — disable password login in your provider panel if offered

Note the **public IP** — you will allowlist it on the Coinbase API key at go-live.

## 2. Base hardening

On the server as root:

```bash
# After cloning the repo to /opt/social-momentum-trader (or your path):
sudo bash scripts/vps-setup.sh
```

This installs Docker, configures `ufw` (SSH only), enables fail2ban, and creates the `smt` user.

## 3. Deploy the application

```bash
sudo su - smt
cd /opt/social-momentum-trader   # or your clone path

git clone <your-repo-url> .     # if not already present
cp .env.production.example .env
nano .env                       # fill REDDIT_*, X_BEARER_TOKEN, POSTGRES_PASSWORD, alerts
```

Edit `config/sources.yaml`:

```yaml
mock:
  enabled: false   # production: real Reddit + X only
```

Build and start:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f trader
```

## 4. Preflight checks

```bash
docker compose exec trader smt doctor
docker compose exec trader smt test-alerts
docker compose exec trader smt soak-report
```

`doctor` must pass before relying on the soak. Fix any `[FAIL]` lines first.

## 5. Paper soak (2+ weeks)

The bot records soak start time in `data/soak.json` on first paper run.

Monitor daily:

```bash
docker compose exec trader smt compare
docker compose exec trader smt soak-report
```

Daily email digests are sent automatically when SMTP is configured (every 24h by default).

**Kill switch from your phone:**

```bash
# On the VPS (SSH or Tailscale):
touch control/KILL
# Or use ntfy/Telegram alert links; see README.
```

## 6. Go live (after soak)

Only when `smt doctor --live` passes:

1. Create Coinbase **trade-only** API key (View + Trade, Transfer off, IP allowlisted).
2. Fund an **isolated portfolio** with small capital.
3. Update `.env`: `COINBASE_*`, `LIVE=true`, `LIVE_ACK=I_UNDERSTAND_LIVE_RISK`
4. `docker compose exec trader smt doctor --live`
5. `docker compose restart trader`

See [go-live-checklist.md](go-live-checklist.md) and [venue.md](venue.md).

## Maintenance

| Task | Command |
|---|---|
| View logs | `docker compose logs -f trader` |
| Restart | `docker compose restart trader` |
| Update code | `git pull && docker compose up -d --build` |
| Backup DB | `docker compose exec postgres pg_dump -U smt smt > backup.sql` |
| Stop trading | `touch control/KILL` |

## Security reminders

- Never commit `.env`
- Postgres bound to `127.0.0.1` only (default in compose)
- No public ports except SSH
- Rotate API keys quarterly or on any suspicion

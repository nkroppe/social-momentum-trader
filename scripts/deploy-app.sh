#!/usr/bin/env bash
# Phase 2: configure and start paper trading on the VPS.
# Run as the smt user from the repo root after .env is filled in.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Missing .env — copy and edit first:"
  echo "  cp .env.production.example .env"
  echo "  nano .env"
  exit 1
fi

echo "==> Disabling mock ingest for production"
sed -i '/^mock:/,/^[^[:space:]]/ s/enabled: true/enabled: false/' config/sources.yaml
grep -A1 '^mock:' config/sources.yaml

echo "==> Creating runtime directories"
mkdir -p data logs control

echo "==> Building and starting stack"
docker compose up -d --build

echo "==> Waiting for trader container"
sleep 8

echo "==> Preflight checks"
docker compose exec -T trader smt doctor

echo
echo "==> Soak report"
docker compose exec -T trader smt soak-report || true

echo
cat <<'EOF'
Next:
  docker compose exec trader smt test-alerts
  docker compose logs -f trader

Paper soak has started (data/soak.json). Check daily:
  docker compose exec trader smt soak-report
EOF

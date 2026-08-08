#!/usr/bin/env bash
# Bootstrap an Ubuntu VPS for social-momentum-trader (paper soak or live).
# Run as root or with sudo on a fresh Ubuntu 22.04/24.04 instance.
#
# Usage:
#   curl -fsSL .../scripts/vps-setup.sh | sudo bash
#   # or after cloning the repo:
#   sudo bash scripts/vps-setup.sh
set -euo pipefail

APP_USER="${APP_USER:-smt}"
APP_DIR="${APP_DIR:-/opt/social-momentum-trader}"
SSH_PORT="${SSH_PORT:-22}"

echo "==> Updating packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq

echo "==> Installing Docker"
if ! command -v docker >/dev/null 2>&1; then
  apt-get install -y -qq ca-certificates curl gnupg ufw fail2ban
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi

echo "==> Creating app user and directories"
if ! id "$APP_USER" &>/dev/null; then
  useradd --create-home --shell /bin/bash "$APP_USER"
fi
mkdir -p "$APP_DIR"/{data,logs,control}
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Configuring firewall (SSH + nothing else public)"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow "${SSH_PORT}/tcp"
ufw --force enable

echo "==> Enabling fail2ban for sshd"
systemctl enable fail2ban
systemctl restart fail2ban

echo "==> Adding $APP_USER to docker group"
usermod -aG docker "$APP_USER"

cat <<EOF

VPS base setup complete.

Next steps (as $APP_USER):
  1. Clone or rsync the repo to $APP_DIR
  2. cp .env.production.example .env   # fill secrets; set POSTGRES_PASSWORD
  3. Set mock.enabled: false in config/sources.yaml
  4. docker compose up -d --build
  5. docker compose exec trader smt doctor
  6. docker compose logs -f trader

See docs/deploy-vps.md for the full checklist.

EOF

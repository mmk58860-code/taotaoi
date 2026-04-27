#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
APP_USER="${APP_USER:-taomonitor}"
APP_GROUP="${APP_GROUP:-taomonitor}"
PORT="${PORT:-8080}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-change-this-password}"
SECRET_KEY="${SECRET_KEY:-change-me}"

sudo getent group "$APP_GROUP" >/dev/null 2>&1 || sudo groupadd --system "$APP_GROUP"
sudo id "$APP_USER" >/dev/null 2>&1 || sudo useradd --system --gid "$APP_GROUP" --create-home --shell /bin/bash "$APP_USER"
sudo install -d -o "$APP_USER" -g "$APP_GROUP" "$APP_DIR"
sudo install -d -o "$APP_USER" -g "$APP_GROUP" "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/backups"
sudo chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"

cd "$APP_DIR"

sudo -u "$APP_USER" python3 -m venv .venv
sudo -u "$APP_USER" .venv/bin/pip install --upgrade pip
sudo -u "$APP_USER" .venv/bin/pip install -r requirements.txt

if [[ ! -f .env ]]; then
  sudo -u "$APP_USER" cp .env.example .env
fi

if [[ -t 0 ]]; then
  read -r -p "Web port [${PORT}]: " input_port || true
  if [[ -n "${input_port:-}" ]]; then
    PORT="$input_port"
  fi

  read -r -p "Admin username [${ADMIN_USERNAME}]: " input_admin_username || true
  if [[ -n "${input_admin_username:-}" ]]; then
    ADMIN_USERNAME="$input_admin_username"
  fi

  read -r -s -p "Admin password [hidden]: " input_admin_password || true
  echo
  if [[ -n "${input_admin_password:-}" ]]; then
    ADMIN_PASSWORD="$input_admin_password"
  fi

  read -r -p "Secret key [auto-generate if blank]: " input_secret_key || true
  if [[ -n "${input_secret_key:-}" ]]; then
    SECRET_KEY="$input_secret_key"
  elif [[ "$SECRET_KEY" == "change-me" ]]; then
    SECRET_KEY="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
  fi
fi

sudo sed -i \
  -e "s|^APP_PORT=.*|APP_PORT=${PORT}|" \
  -e "s|^ADMIN_USERNAME=.*|ADMIN_USERNAME=${ADMIN_USERNAME}|" \
  -e "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${ADMIN_PASSWORD}|" \
  -e "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET_KEY}|" \
  .env

sudo sed "s|WorkingDirectory=/opt/tao-monitor|WorkingDirectory=$APP_DIR|; s|EnvironmentFile=/opt/tao-monitor/.env|EnvironmentFile=$APP_DIR/.env|; s|ExecStart=/opt/tao-monitor/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080|ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $PORT|; s|User=taomonitor|User=$APP_USER|; s|Group=taomonitor|Group=$APP_GROUP|" \
  deploy/systemd/tao-monitor.service | sudo tee /etc/systemd/system/tao-monitor.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable tao-monitor.service
sudo systemctl restart tao-monitor.service
sudo systemctl status tao-monitor.service --no-pager
echo
echo "TAO Monitor deployed."
echo "Web URL: http://$(hostname -I | awk '{print $1}'):${PORT}"
echo "Admin username: ${ADMIN_USERNAME}"

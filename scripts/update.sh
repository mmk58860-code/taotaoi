#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
APP_USER="${APP_USER:-taomonitor}"

cd "$APP_DIR"

./scripts/backup.sh
sudo -u "$APP_USER" git fetch --all
sudo -u "$APP_USER" git pull --ff-only
sudo -u "$APP_USER" .venv/bin/pip install -r requirements.txt
sudo systemctl restart tao-monitor.service
sudo systemctl status tao-monitor.service --no-pager

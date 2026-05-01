#!/usr/bin/env bash
set -euo pipefail

# 仍然以项目根目录为基准执行更新，避免相对路径错乱。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
APP_USER="${APP_USER:-taomonitor}"

run_privileged() {
  # 兼容有 sudo 和无 sudo 的服务器环境。
  if command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    "$@"
  fi
}

run_as_app_user() {
  # 代码拉取和 pip 安装尽量用应用用户执行。
  if [[ "$(id -un)" == "$APP_USER" ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -u "$APP_USER" "$@"
  elif command -v runuser >/dev/null 2>&1; then
    runuser -u "$APP_USER" -- "$@"
  elif command -v su >/dev/null 2>&1; then
    su -s /bin/bash "$APP_USER" -c "$(printf '%q ' "$@")"
  else
    echo "Cannot switch to app user $APP_USER" >&2
    exit 1
  fi
}

cd "$APP_DIR"

# 更新前先备份数据库，避免误更新导致资料丢失。
./scripts/backup.sh
run_as_app_user git fetch --all
run_as_app_user git pull --ff-only
run_as_app_user .venv/bin/pip install -r requirements.txt
run_as_app_user .venv/bin/alembic upgrade head
run_as_app_user python3 - "$APP_DIR/.env" <<'PY'
from pathlib import Path
import sys

env_path = Path(sys.argv[1])
updates = {
    "CLEANUP_RETENTION_HOURS": "1",
    "CLEANUP_INTERVAL_MINUTES": "10",
    "TAOSTATS_ENABLED": "false",
    "TAOSTATS_API_KEY": "",
    "TAOSTATS_API_KEYS": "",
    "TAOSTATS_AMOUNT_MODE": "fallback",
    "TAOSTATS_REQUEST_INTERVAL_SECONDS": "2",
    "TAOSTATS_RATE_LIMIT_COOLDOWN_SECONDS": "60",
    "TAOSTATS_RETRY_COOLDOWN_SECONDS": "120",
}
preserve_existing = {
    "TAOSTATS_ENABLED",
    "TAOSTATS_API_KEY",
    "TAOSTATS_API_KEYS",
    "TAOSTATS_AMOUNT_MODE",
    "TAOSTATS_REQUEST_INTERVAL_SECONDS",
    "TAOSTATS_RATE_LIMIT_COOLDOWN_SECONDS",
    "TAOSTATS_RETRY_COOLDOWN_SECONDS",
}
lines = env_path.read_text(encoding="utf-8").splitlines()
seen = set()
out = []
for line in lines:
    if "=" not in line or line.lstrip().startswith("#"):
        out.append(line)
        continue
    key, _, _ = line.partition("=")
    if key in updates:
        current_value = line.partition("=")[2]
        value = current_value if key in preserve_existing and current_value else updates[key]
        out.append(f"{key}={value}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
run_privileged systemctl restart tao-monitor.service
run_privileged systemctl status tao-monitor.service --no-pager

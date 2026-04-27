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
run_privileged systemctl restart tao-monitor.service
run_privileged systemctl status tao-monitor.service --no-pager

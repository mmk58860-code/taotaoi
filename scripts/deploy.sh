#!/usr/bin/env bash
set -euo pipefail

# 计算脚本所在目录，保证从任意位置执行都能定位到项目根目录。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
APP_USER="${APP_USER:-taomonitor}"
APP_GROUP="${APP_GROUP:-taomonitor}"
PORT="${PORT:-8080}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-change-this-password}"
SECRET_KEY="${SECRET_KEY:-change-me}"

run_privileged() {
  # 如果服务器有 sudo 就用 sudo，否则直接执行，兼容 root 机器。
  if command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    "$@"
  fi
}

run_as_app_user() {
  # 尽量以应用用户运行 pip 和 git，避免生成 root 权限文件。
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

# 确保服务运行用户、目录和数据目录都准备齐全。
run_privileged getent group "$APP_GROUP" >/dev/null 2>&1 || run_privileged groupadd --system "$APP_GROUP"
run_privileged id "$APP_USER" >/dev/null 2>&1 || run_privileged useradd --system --gid "$APP_GROUP" --create-home --shell /bin/bash "$APP_USER"
run_privileged install -d -o "$APP_USER" -g "$APP_GROUP" "$APP_DIR"
run_privileged install -d -o "$APP_USER" -g "$APP_GROUP" "$APP_DIR/data" "$APP_DIR/logs" "$APP_DIR/backups"
run_privileged chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"

cd "$APP_DIR"

# 初始化虚拟环境并安装项目依赖。
run_as_app_user python3 -m venv .venv
run_as_app_user .venv/bin/pip install --upgrade pip
run_as_app_user .venv/bin/pip install -r requirements.txt

if [[ ! -f .env ]]; then
  # 第一次部署时，用示例配置生成正式 .env。
  run_as_app_user cp .env.example .env
fi

if [[ -t 0 ]]; then
  # 交互模式下，允许用户现场设置端口和总管理员信息。
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

# 把交互输入安全写回 .env，避免密码里带特殊字符时把配置文件写坏。
run_as_app_user python3 - "$APP_DIR/.env" "$PORT" "$ADMIN_USERNAME" "$ADMIN_PASSWORD" "$SECRET_KEY" <<'PY'
from pathlib import Path
import sys

env_path = Path(sys.argv[1])
updates = {
    "APP_PORT": sys.argv[2],
    "ADMIN_USERNAME": sys.argv[3],
    "ADMIN_PASSWORD": sys.argv[4],
    "SECRET_KEY": sys.argv[5],
}

lines = env_path.read_text(encoding="utf-8").splitlines()
seen = set()
result = []

for line in lines:
    if "=" not in line or line.lstrip().startswith("#"):
        result.append(line)
        continue
    key, _, _ = line.partition("=")
    if key in updates:
        result.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        result.append(line)

for key, value in updates.items():
    if key not in seen:
        result.append(f"{key}={value}")

env_path.write_text("\n".join(result) + "\n", encoding="utf-8")
PY

# 根据当前项目实际路径生成 systemd 服务文件。
run_privileged bash -lc "$(cat <<EOF
sed "s|WorkingDirectory=/opt/tao-monitor|WorkingDirectory=$APP_DIR|; s|EnvironmentFile=/opt/tao-monitor/.env|EnvironmentFile=$APP_DIR/.env|; s|ExecStart=/opt/tao-monitor/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080|ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $PORT|; s|User=taomonitor|User=$APP_USER|; s|Group=taomonitor|Group=$APP_GROUP|" "$APP_DIR/deploy/systemd/tao-monitor.service" > /etc/systemd/system/tao-monitor.service
EOF
)"
run_privileged systemctl daemon-reload
run_privileged systemctl enable tao-monitor.service
run_privileged systemctl restart tao-monitor.service
run_privileged systemctl status tao-monitor.service --no-pager
echo
echo "TAO Monitor deployed."
echo "Web URL: http://$(hostname -I | awk '{print $1}'):${PORT}"
echo "Admin username: ${ADMIN_USERNAME}"

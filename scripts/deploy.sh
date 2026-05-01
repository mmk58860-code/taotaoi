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
DB_NAME="${DB_NAME:-tao_monitor}"
DB_USER="${DB_USER:-taomonitor}"
DB_PASSWORD="${DB_PASSWORD:-}"

run_privileged() {
  # 如果服务器有 sudo 就用 sudo，否则直接执行，兼容 root 机器。
  if command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    "$@"
  fi
}

run_as_app_user() {
  # 尽量以应用用户运行 pip、git 和迁移命令，避免生成 root 权限文件。
  if [[ "$(id -un)" == "$APP_USER" ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -u "$APP_USER" "$@"
  elif command -v runuser >/dev/null 2>&1; then
    runuser -u "$APP_USER" -- "$@"
  elif command -v su >/dev/null 2>&1; then
    su -s /bin/bash "$APP_USER" -c "$(printf '%q ' "$@")"
  else
    echo "无法切换到应用用户 $APP_USER" >&2
    exit 1
  fi
}

run_as_postgres() {
  # PostgreSQL 初始化需要 postgres 系统用户权限。
  if command -v sudo >/dev/null 2>&1; then
    sudo -u postgres "$@"
  elif command -v runuser >/dev/null 2>&1; then
    runuser -u postgres -- "$@"
  else
    su -s /bin/bash postgres -c "$(printf '%q ' "$@")"
  fi
}

if command -v apt-get >/dev/null 2>&1; then
  run_privileged apt-get update
  run_privileged apt-get install -y python3 python3-venv python3-pip git rsync zip postgresql postgresql-client
fi

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

CURRENT_DB_PASSWORD="$(
  python3 - "$APP_DIR/.env" <<'PY'
from pathlib import Path
from urllib.parse import unquote, urlparse
import sys

env_path = Path(sys.argv[1])
values = {}
if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key] = value
raw_url = values.get("DATABASE_URL", "").replace("postgresql+psycopg2://", "postgresql://")
parsed = urlparse(raw_url)
print(unquote(parsed.password or ""))
PY
)"
if [[ -z "$DB_PASSWORD" && -n "$CURRENT_DB_PASSWORD" && "$CURRENT_DB_PASSWORD" != "change-this-password" ]]; then
  DB_PASSWORD="$CURRENT_DB_PASSWORD"
fi

if [[ -t 0 ]]; then
  # 交互模式下，允许用户现场设置端口、总管理员信息和数据库密码。
  read -r -p "网页端口 [${PORT}]: " input_port || true
  if [[ -n "${input_port:-}" ]]; then
    PORT="$input_port"
  fi

  read -r -p "总管理员账号 [${ADMIN_USERNAME}]: " input_admin_username || true
  if [[ -n "${input_admin_username:-}" ]]; then
    ADMIN_USERNAME="$input_admin_username"
  fi

  read -r -s -p "总管理员密码 [输入时隐藏]: " input_admin_password || true
  echo
  if [[ -n "${input_admin_password:-}" ]]; then
    ADMIN_PASSWORD="$input_admin_password"
  fi

  read -r -s -p "PostgreSQL 数据库密码 [留空自动生成]: " input_db_password || true
  echo
  if [[ -n "${input_db_password:-}" ]]; then
    DB_PASSWORD="$input_db_password"
  fi

  read -r -p "会话密钥 SECRET_KEY [留空则自动生成]: " input_secret_key || true
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

if [[ -z "$DB_PASSWORD" || "$DB_PASSWORD" == "change-this-password" ]]; then
  DB_PASSWORD="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
fi

run_privileged systemctl enable --now postgresql || true

if id postgres >/dev/null 2>&1; then
  escaped_db_password="${DB_PASSWORD//\'/\'\'}"
  run_as_postgres psql <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${escaped_db_password}';
  ELSE
    ALTER ROLE ${DB_USER} WITH PASSWORD '${escaped_db_password}';
  END IF;
END
\$\$;
SQL

  if ! run_as_postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
    run_as_postgres createdb -O "$DB_USER" "$DB_NAME"
  fi
fi

# 把交互输入安全写回 .env，避免密码里带特殊字符时把配置文件写坏。
run_as_app_user python3 - "$APP_DIR/.env" "$PORT" "$ADMIN_USERNAME" "$ADMIN_PASSWORD" "$SECRET_KEY" "$DB_USER" "$DB_PASSWORD" "$DB_NAME" <<'PY'
from pathlib import Path
from urllib.parse import quote
import sys

env_path = Path(sys.argv[1])
port, admin_username, admin_password, secret_key, db_user, db_password, db_name = sys.argv[2:9]
database_url = f"postgresql+psycopg2://{db_user}:{quote(db_password)}@127.0.0.1:5432/{db_name}"
updates = {
    "APP_PORT": port,
    "ADMIN_USERNAME": admin_username,
    "ADMIN_PASSWORD": admin_password,
    "SECRET_KEY": secret_key,
    "DATABASE_URL": database_url,
    "TAOSTATS_ENABLED": "false",
    "TAOSTATS_API_KEY": "",
    "TAOSTATS_API_KEYS": "",
    "TAOSTATS_AMOUNT_MODE": "fallback",
    "TAOSTATS_REQUEST_INTERVAL_SECONDS": "2",
    "TAOSTATS_RATE_LIMIT_COOLDOWN_SECONDS": "60",
    "TAOSTATS_RETRY_COOLDOWN_SECONDS": "120",
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

run_as_app_user .venv/bin/alembic upgrade head

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
echo "TAO Monitor 部署完成。"
echo "网页地址: http://$(hostname -I | awk '{print $1}'):${PORT}"
echo "总管理员账号: ${ADMIN_USERNAME}"

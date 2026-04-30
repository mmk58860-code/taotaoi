#!/usr/bin/env bash
set -euo pipefail

# 以脚本所在位置为基准，兼容从任意目录执行。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
README_FILE="${README_FILE:-$APP_DIR/README.md}"

mkdir -p "$BACKUP_DIR"

timestamp="$(date +%Y%m%d-%H%M%S)"
staging_dir="$BACKUP_DIR/backup-$timestamp"
archive_file="$BACKUP_DIR/tao-monitor-backup-$timestamp.zip"

mkdir -p "$staging_dir"

DATABASE_URL_VALUE="$(
  python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import sys

env_path = Path(sys.argv[1])
if not env_path.exists():
    print("")
    raise SystemExit
for line in env_path.read_text(encoding="utf-8").splitlines():
    if "=" not in line or line.lstrip().startswith("#"):
        continue
    key, _, value = line.partition("=")
    if key == "DATABASE_URL":
        print(value.strip())
        break
else:
    print("")
PY
)"

if [[ "$DATABASE_URL_VALUE" == postgresql* ]]; then
  if ! command -v pg_dump >/dev/null 2>&1; then
    echo "pg_dump 未安装，无法备份 PostgreSQL 数据库" >&2
    exit 1
  fi
  pg_dump_url="${DATABASE_URL_VALUE/postgresql+psycopg2:\/\//postgresql:\/\/}"
  pg_dump "$pg_dump_url" --format=custom --file "$staging_dir/tao_monitor.dump"
elif [[ -f "$APP_DIR/data/tao_monitor.db" ]]; then
  # 仅兼容旧版 SQLite 部署；新部署会走 PostgreSQL dump。
  python3 - "$APP_DIR/data/tao_monitor.db" "$staging_dir/tao_monitor.db" <<'PY'
import sqlite3
import sys

source_path = sys.argv[1]
target_path = sys.argv[2]
source = sqlite3.connect(source_path)
target = sqlite3.connect(target_path)
with target:
    source.backup(target)
target.close()
source.close()
PY
fi

if [[ -f "$ENV_FILE" ]]; then
  cp "$ENV_FILE" "$staging_dir/.env"
fi

if [[ -f "$README_FILE" ]]; then
  cp "$README_FILE" "$staging_dir/README.md"
fi

(
  cd "$staging_dir"
  zip -qr "$archive_file" .
)

rm -rf "$staging_dir"

echo "backup created: $archive_file"

#!/usr/bin/env bash
set -euo pipefail

# 以脚本所在位置为基准，兼容从任意目录执行。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
DB_FILE="${DB_FILE:-$APP_DIR/data/tao_monitor.db}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env}"
README_FILE="${README_FILE:-$APP_DIR/README.md}"

mkdir -p "$BACKUP_DIR"

timestamp="$(date +%Y%m%d-%H%M%S)"
staging_dir="$BACKUP_DIR/backup-$timestamp"
archive_file="$BACKUP_DIR/tao-monitor-backup-$timestamp.zip"

mkdir -p "$staging_dir"

# 备份核心资料：数据库、环境配置、项目说明。
if [[ -f "$DB_FILE" ]]; then
  # 使用 SQLite 自带 backup API 生成一致性副本，避免运行中直接 cp 导致文件不完整。
  python3 - "$DB_FILE" "$staging_dir/tao_monitor.db" <<'PY'
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

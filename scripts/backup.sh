#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/tao-monitor}"
DB_FILE="${DB_FILE:-$APP_DIR/data/tao_monitor.db}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"

mkdir -p "$BACKUP_DIR"

if [[ -f "$DB_FILE" ]]; then
  ts="$(date +%Y%m%d-%H%M%S)"
  cp "$DB_FILE" "$BACKUP_DIR/tao_monitor-$ts.db"
  echo "backup created: $BACKUP_DIR/tao_monitor-$ts.db"
else
  echo "database not found: $DB_FILE"
fi


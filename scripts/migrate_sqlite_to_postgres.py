#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
from psycopg2.extras import execute_values


TABLES = (
    "admin_users",
    "monitor_menus",
    "wallet_watches",
    "chain_events",
    "monitor_state",
    "app_settings",
    "user_settings",
    "notification_outbox",
)

BOOL_COLUMNS = {
    "admin_users": {"is_superadmin"},
    "monitor_menus": {"is_builtin"},
    "wallet_watches": {"enabled"},
    "chain_events": {"success", "notification_sent"},
}

SERIAL_TABLES = {
    "admin_users": "id",
    "monitor_menus": "id",
    "wallet_watches": "id",
    "chain_events": "id",
    "notification_outbox": "id",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="把旧 SQLite 数据安全迁移到 PostgreSQL。")
    parser.add_argument("--sqlite", default="data/tao_monitor.db", help="旧 SQLite 数据库文件路径")
    parser.add_argument("--postgres-url", default=os.environ.get("TARGET_DATABASE_URL", ""), help="目标 PostgreSQL DATABASE_URL")
    parser.add_argument("--replace", action="store_true", help="目标库已有数据时先清空再导入")
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite)
    postgres_url = normalize_postgres_url(args.postgres_url)
    if not sqlite_path.exists():
        print(f"SQLite 数据库不存在：{sqlite_path}", file=sys.stderr)
        return 1
    if not postgres_url:
        print("请通过 --postgres-url 或 TARGET_DATABASE_URL 指定 PostgreSQL 连接地址", file=sys.stderr)
        return 1
    if not postgres_url.startswith("postgresql://"):
        print("目标地址必须是 PostgreSQL DATABASE_URL", file=sys.stderr)
        return 1

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = psycopg2.connect(postgres_url)
    try:
        with pg_conn:
            with pg_conn.cursor() as cursor:
                existing_rows = count_existing_rows(cursor)
                if existing_rows and not args.replace:
                    print(
                        f"目标 PostgreSQL 已有 {existing_rows} 条数据。确认要覆盖时加 --replace。",
                        file=sys.stderr,
                    )
                    return 1
                if args.replace:
                    truncate_tables(cursor)

                for table in TABLES:
                    copy_table(sqlite_conn, cursor, table)
                reset_sequences(cursor)
    finally:
        pg_conn.close()
        sqlite_conn.close()

    print("SQLite 数据已迁移到 PostgreSQL。")
    return 0


def normalize_postgres_url(raw_url: str) -> str:
    raw_url = raw_url.strip()
    if raw_url.startswith("postgresql+psycopg2://"):
        return raw_url.replace("postgresql+psycopg2://", "postgresql://", 1)
    return raw_url


def count_existing_rows(cursor) -> int:
    total = 0
    for table in TABLES:
        if not postgres_table_exists(cursor, table):
            continue
        cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
        total += int(cursor.fetchone()[0])
    return total


def truncate_tables(cursor) -> None:
    existing = [table for table in TABLES if postgres_table_exists(cursor, table)]
    if existing:
        joined = ", ".join(f'"{table}"' for table in reversed(existing))
        cursor.execute(f"TRUNCATE {joined} RESTART IDENTITY CASCADE")


def copy_table(sqlite_conn: sqlite3.Connection, cursor, table: str) -> None:
    if not sqlite_table_exists(sqlite_conn, table) or not postgres_table_exists(cursor, table):
        return

    rows = sqlite_conn.execute(f'SELECT * FROM "{table}"').fetchall()
    if not rows:
        return

    columns = rows[0].keys()
    bool_columns = BOOL_COLUMNS.get(table, set())
    values = []
    for row in rows:
        converted = []
        for column in columns:
            value = row[column]
            if column in bool_columns and value is not None:
                value = bool(value)
            converted.append(value)
        values.append(tuple(converted))

    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    query = f'INSERT INTO "{table}" ({quoted_columns}) VALUES %s'
    execute_values(cursor, query, values)
    print(f"已迁移 {table}: {len(values)} 条")


def reset_sequences(cursor) -> None:
    for table, column in SERIAL_TABLES.items():
        if not postgres_table_exists(cursor, table):
            continue
        cursor.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{table}', '{column}'),
                COALESCE((SELECT MAX("{column}") FROM "{table}"), 1),
                (SELECT MAX("{column}") FROM "{table}") IS NOT NULL
            )
            """
        )


def sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def postgres_table_exists(cursor, table: str) -> bool:
    cursor.execute("SELECT to_regclass(%s)", (f"public.{table}",))
    return cursor.fetchone()[0] is not None


if __name__ == "__main__":
    raise SystemExit(main())

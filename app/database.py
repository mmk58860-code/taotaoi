from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import BASE_DIR, get_settings


# 所有 ORM 模型都继承这个基类。
class Base(DeclarativeBase):
    pass


settings = get_settings()

if settings.database_url.startswith("sqlite:///"):
    # SQLite 模式下，先确保数据库目录存在。
    db_path = BASE_DIR / settings.database_url.removeprefix("sqlite:///")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

# 创建数据库引擎；SQLite 需要关闭跨线程限制。
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Session:
    # 统一的数据库会话上下文：成功就提交，失败就回滚。
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_startup_migrations(superadmin_user_id: int) -> None:
    # 旧版本是单账号结构，这里在启动时尽量平滑升级到多监控菜单结构。
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    with engine.begin() as connection:
        if "admin_users" in table_names:
            admin_columns = {column["name"] for column in inspector.get_columns("admin_users")}
            if "password_ciphertext" not in admin_columns:
                connection.execute(text("ALTER TABLE admin_users ADD COLUMN password_ciphertext TEXT DEFAULT ''"))

        if "monitor_menus" not in table_names:
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS monitor_menus (
                        id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                        owner_user_id INTEGER NOT NULL,
                        name VARCHAR(128) NOT NULL,
                        menu_kind VARCHAR(32) NOT NULL,
                        is_builtin BOOLEAN NOT NULL DEFAULT 0,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        large_transfer_threshold_tao FLOAT NOT NULL DEFAULT 0,
                        telegram_bot_token VARCHAR(256) NOT NULL DEFAULT '',
                        telegram_chat_id VARCHAR(128) NOT NULL DEFAULT '',
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL
                    )
                    """
                )
            )
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_monitor_menus_owner_user_id ON monitor_menus (owner_user_id)"))

        owner_user_ids = [
            int(row["id"])
            for row in connection.execute(text("SELECT id FROM admin_users ORDER BY id ASC")).mappings().all()
        ]
        if not owner_user_ids and superadmin_user_id:
            owner_user_ids = [superadmin_user_id]
        for owner_user_id in owner_user_ids:
            _ensure_builtin_monitor_menus_sql(connection, owner_user_id)

        if "wallet_watches" in table_names:
            wallet_columns = {column["name"] for column in inspector.get_columns("wallet_watches")}
            if (
                "owner_user_id" not in wallet_columns
                or "monitor_menu_id" not in wallet_columns
                or not _has_index(connection, "wallet_watches", "uq_wallet_menu_address")
            ):
                _rebuild_wallet_watches(connection, superadmin_user_id)

        if "chain_events" in table_names:
            event_columns = {column["name"] for column in inspector.get_columns("chain_events")}
            if (
                "owner_user_id" not in event_columns
                or "monitor_menu_id" not in event_columns
                or not _has_index(connection, "chain_events", "uq_menu_block_event")
            ):
                _rebuild_chain_events(connection, superadmin_user_id)
                event_columns = _get_sqlite_columns(connection, "chain_events")
            _ensure_sqlite_column(
                connection,
                table_name="chain_events",
                existing_columns=event_columns,
                column_name="extrinsic_index",
                column_definition="INTEGER NOT NULL DEFAULT 0",
            )
            _ensure_sqlite_column(
                connection,
                table_name="chain_events",
                existing_columns=event_columns,
                column_name="action_type",
                column_definition="VARCHAR(64) NOT NULL DEFAULT 'generic_call'",
            )
            _ensure_sqlite_column(
                connection,
                table_name="chain_events",
                existing_columns=event_columns,
                column_name="call_name",
                column_definition="VARCHAR(96) NOT NULL DEFAULT ''",
            )
            _ensure_sqlite_column(
                connection,
                table_name="chain_events",
                existing_columns=event_columns,
                column_name="signer_address",
                column_definition="VARCHAR(128)",
            )
            _ensure_sqlite_column(
                connection,
                table_name="chain_events",
                existing_columns=event_columns,
                column_name="success",
                column_definition="BOOLEAN NOT NULL DEFAULT 1",
            )
            _ensure_sqlite_column(
                connection,
                table_name="chain_events",
                existing_columns=event_columns,
                column_name="failure_reason",
                column_definition="TEXT",
            )
            _ensure_sqlite_column(
                connection,
                table_name="chain_events",
                existing_columns=event_columns,
                column_name="involved_addresses_json",
                column_definition="TEXT NOT NULL DEFAULT '[]'",
            )
            _ensure_sqlite_column(
                connection,
                table_name="chain_events",
                existing_columns=event_columns,
                column_name="matched_aliases_json",
                column_definition="TEXT NOT NULL DEFAULT '[]'",
            )
            _ensure_index(connection, "chain_events", "ix_chain_events_signer_address", "CREATE INDEX ix_chain_events_signer_address ON chain_events (signer_address)")


def _has_index(connection, table_name: str, index_name: str) -> bool:
    # SQLite 的索引结构用 PRAGMA 读取，方便判断是不是旧表结构。
    rows = connection.execute(text(f"PRAGMA index_list('{table_name}')")).mappings().all()
    return any(row.get("name") == index_name for row in rows)


def _ensure_sqlite_column(connection, table_name: str, existing_columns: set[str], column_name: str, column_definition: str) -> None:
    # SQLite 只能追加列，这里按缺失情况补齐新版本需要的字段。
    if column_name in existing_columns:
        return
    connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"))
    existing_columns.add(column_name)


def _get_sqlite_columns(connection, table_name: str) -> set[str]:
    # 在同一连接上读取表结构，避免 SQLite 迁移事务里再次开连接造成锁冲突。
    return {
        row["name"]
        for row in connection.execute(text(f"PRAGMA table_info('{table_name}')")).mappings().all()
    }


def _ensure_index(connection, table_name: str, index_name: str, create_sql: str) -> None:
    # 有些新增字段需要索引，但旧库里不一定存在。
    if _has_index(connection, table_name, index_name):
        return
    connection.execute(text(create_sql))


def _rebuild_wallet_watches(connection, superadmin_user_id: int) -> None:
    # 重建钱包表，改成“同一监控菜单内地址唯一”。
    connection.execute(text("ALTER TABLE wallet_watches RENAME TO wallet_watches_legacy"))
    _drop_indexes_if_exist(
        connection,
        [
            "ix_wallet_watches_address",
            "ix_wallet_watches_owner_user_id",
            "ix_wallet_watches_monitor_menu_id",
            "uq_wallet_owner_address",
            "uq_wallet_menu_address",
        ],
    )
    connection.execute(
        text(
            """
            CREATE TABLE wallet_watches (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                monitor_menu_id INTEGER NOT NULL,
                address VARCHAR(128) NOT NULL,
                alias VARCHAR(128) NOT NULL,
                enabled BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL
            )
            """
        )
    )
    connection.execute(text("CREATE INDEX ix_wallet_watches_address ON wallet_watches (address)"))
    connection.execute(text("CREATE INDEX ix_wallet_watches_owner_user_id ON wallet_watches (owner_user_id)"))
    connection.execute(text("CREATE INDEX ix_wallet_watches_monitor_menu_id ON wallet_watches (monitor_menu_id)"))
    connection.execute(text("CREATE UNIQUE INDEX uq_wallet_menu_address ON wallet_watches (monitor_menu_id, address)"))
    legacy_columns = {
        row["name"] for row in connection.execute(text("PRAGMA table_info('wallet_watches_legacy')")).mappings().all()
    }
    owner_column = "owner_user_id" if "owner_user_id" in legacy_columns else str(superadmin_user_id)
    monitor_menu_expr = (
        "COALESCE(monitor_menu_id, (SELECT id FROM monitor_menus WHERE owner_user_id = COALESCE(wallet_watches_legacy.owner_user_id, :owner_user_id) AND is_builtin = 1 AND menu_kind = 'wallet' LIMIT 1))"
        if "monitor_menu_id" in legacy_columns
        else "(SELECT id FROM monitor_menus WHERE owner_user_id = COALESCE(wallet_watches_legacy.owner_user_id, :owner_user_id) AND is_builtin = 1 AND menu_kind = 'wallet' LIMIT 1)"
    )
    connection.execute(
        text(
            f"""
            INSERT INTO wallet_watches (id, owner_user_id, monitor_menu_id, address, alias, enabled, created_at)
            SELECT id, COALESCE({owner_column}, :owner_user_id), {monitor_menu_expr}, address, alias, enabled, created_at
            FROM wallet_watches_legacy
            """
        ),
        {"owner_user_id": superadmin_user_id},
    )
    connection.execute(text("DROP TABLE wallet_watches_legacy"))


def _rebuild_chain_events(connection, superadmin_user_id: int) -> None:
    # 重建事件表，把历史事件迁到对应账号的钱包监控菜单下。
    connection.execute(text("ALTER TABLE chain_events RENAME TO chain_events_legacy"))
    _drop_indexes_if_exist(
        connection,
        [
            "ix_chain_events_owner_user_id",
            "ix_chain_events_monitor_menu_id",
            "ix_chain_events_block_number",
            "ix_chain_events_from_address",
            "ix_chain_events_to_address",
            "ix_chain_events_signer_address",
            "ix_chain_events_detected_at",
            "uq_owner_block_event",
            "uq_menu_block_event",
        ],
    )
    connection.execute(
        text(
            """
            CREATE TABLE chain_events (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                monitor_menu_id INTEGER NOT NULL,
                block_number INTEGER NOT NULL,
                event_index INTEGER NOT NULL,
                extrinsic_index INTEGER NOT NULL DEFAULT 0,
                pallet VARCHAR(64) NOT NULL,
                event_name VARCHAR(64) NOT NULL,
                action_type VARCHAR(64) NOT NULL DEFAULT 'generic_call',
                call_name VARCHAR(96) NOT NULL DEFAULT '',
                amount_tao FLOAT NOT NULL,
                from_address VARCHAR(128),
                to_address VARCHAR(128),
                signer_address VARCHAR(128),
                extrinsic_hash VARCHAR(128),
                success BOOLEAN NOT NULL DEFAULT 1,
                failure_reason TEXT,
                involved_addresses_json TEXT NOT NULL DEFAULT '[]',
                matched_aliases_json TEXT NOT NULL DEFAULT '[]',
                message TEXT NOT NULL,
                raw_payload TEXT NOT NULL,
                notification_sent BOOLEAN NOT NULL,
                detected_at DATETIME NOT NULL
            )
            """
        )
    )
    connection.execute(text("CREATE INDEX ix_chain_events_owner_user_id ON chain_events (owner_user_id)"))
    connection.execute(text("CREATE INDEX ix_chain_events_monitor_menu_id ON chain_events (monitor_menu_id)"))
    connection.execute(text("CREATE INDEX ix_chain_events_block_number ON chain_events (block_number)"))
    connection.execute(text("CREATE INDEX ix_chain_events_from_address ON chain_events (from_address)"))
    connection.execute(text("CREATE INDEX ix_chain_events_to_address ON chain_events (to_address)"))
    connection.execute(text("CREATE INDEX ix_chain_events_signer_address ON chain_events (signer_address)"))
    connection.execute(text("CREATE INDEX ix_chain_events_detected_at ON chain_events (detected_at)"))
    connection.execute(text("CREATE UNIQUE INDEX uq_menu_block_event ON chain_events (monitor_menu_id, block_number, event_index)"))
    legacy_columns = {
        row["name"] for row in connection.execute(text("PRAGMA table_info('chain_events_legacy')")).mappings().all()
    }
    owner_column = "owner_user_id" if "owner_user_id" in legacy_columns else str(superadmin_user_id)
    monitor_menu_expr = (
        "COALESCE(monitor_menu_id, (SELECT id FROM monitor_menus WHERE owner_user_id = COALESCE(chain_events_legacy.owner_user_id, :owner_user_id) AND is_builtin = 1 AND menu_kind = 'wallet' LIMIT 1))"
        if "monitor_menu_id" in legacy_columns
        else "(SELECT id FROM monitor_menus WHERE owner_user_id = COALESCE(chain_events_legacy.owner_user_id, :owner_user_id) AND is_builtin = 1 AND menu_kind = 'wallet' LIMIT 1)"
    )
    extrinsic_index_expr = "extrinsic_index" if "extrinsic_index" in legacy_columns else "0"
    action_type_expr = "action_type" if "action_type" in legacy_columns else "'generic_call'"
    call_name_expr = "call_name" if "call_name" in legacy_columns else "event_name"
    signer_expr = "signer_address" if "signer_address" in legacy_columns else "NULL"
    success_expr = "success" if "success" in legacy_columns else "1"
    failure_expr = "failure_reason" if "failure_reason" in legacy_columns else "NULL"
    involved_expr = "involved_addresses_json" if "involved_addresses_json" in legacy_columns else "'[]'"
    matched_expr = "matched_aliases_json" if "matched_aliases_json" in legacy_columns else "'[]'"
    connection.execute(
        text(
            f"""
            INSERT INTO chain_events (
                id, owner_user_id, monitor_menu_id, block_number, event_index, extrinsic_index, pallet, event_name,
                action_type, call_name, amount_tao, from_address, to_address, signer_address, extrinsic_hash,
                success, failure_reason, involved_addresses_json, matched_aliases_json,
                message, raw_payload, notification_sent, detected_at
            )
            SELECT
                id, COALESCE({owner_column}, :owner_user_id), {monitor_menu_expr}, block_number, event_index, {extrinsic_index_expr}, pallet, event_name,
                {action_type_expr}, {call_name_expr}, amount_tao, from_address, to_address, {signer_expr}, extrinsic_hash,
                {success_expr}, {failure_expr}, {involved_expr}, {matched_expr},
                message, raw_payload, notification_sent, detected_at
            FROM chain_events_legacy
            """
        ),
        {"owner_user_id": superadmin_user_id},
    )
    connection.execute(text("DROP TABLE chain_events_legacy"))


def _drop_indexes_if_exist(connection, index_names: list[str]) -> None:
    # SQLite 在 rename table 后会保留旧索引名，重建同名索引前需要先清掉。
    for index_name in index_names:
        connection.execute(text(f"DROP INDEX IF EXISTS {index_name}"))


def _ensure_builtin_monitor_menus_sql(connection, owner_user_id: int) -> None:
    # 启动迁移时直接保证每个账号至少有两个基础菜单。
    connection.execute(
        text(
            """
            INSERT INTO monitor_menus (
                owner_user_id, name, menu_kind, is_builtin, sort_order,
                large_transfer_threshold_tao, telegram_bot_token, telegram_chat_id, created_at, updated_at
            )
            SELECT :owner_user_id, '大额预警', 'alert', 1, 20, 5.0, '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            WHERE NOT EXISTS (
                SELECT 1 FROM monitor_menus
                WHERE owner_user_id = :owner_user_id AND is_builtin = 1 AND menu_kind = 'alert'
            )
            """
        ),
        {"owner_user_id": owner_user_id},
    )
    connection.execute(
        text(
            """
            INSERT INTO monitor_menus (
                owner_user_id, name, menu_kind, is_builtin, sort_order,
                large_transfer_threshold_tao, telegram_bot_token, telegram_chat_id, created_at, updated_at
            )
            SELECT :owner_user_id, '钱包监控', 'wallet', 1, 30, 0.0, '', '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            WHERE NOT EXISTS (
                SELECT 1 FROM monitor_menus
                WHERE owner_user_id = :owner_user_id AND is_builtin = 1 AND menu_kind = 'wallet'
            )
            """
        ),
        {"owner_user_id": owner_user_id},
    )

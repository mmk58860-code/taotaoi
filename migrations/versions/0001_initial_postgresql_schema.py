"""初始化 PostgreSQL 数据库结构

Revision ID: 0001_initial_postgresql_schema
Revises:
Create Date: 2026-04-30
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial_postgresql_schema"
down_revision = None
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # 每个建表动作都先判断是否存在，方便接管旧库时直接 stamp/upgrade。
    if not _has_table("admin_users"):
        op.create_table(
            "admin_users",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("username", sa.String(length=64), nullable=False),
            sa.Column("password_hash", sa.String(length=256), nullable=False),
            sa.Column("password_ciphertext", sa.Text(), nullable=False, server_default=""),
            sa.Column("is_superadmin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("username"),
        )
        op.create_index("ix_admin_users_username", "admin_users", ["username"], unique=False)

    if not _has_table("monitor_menus"):
        op.create_table(
            "monitor_menus",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("owner_user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("menu_kind", sa.String(length=32), nullable=False),
            sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("large_transfer_threshold_tao", sa.Float(), nullable=False, server_default="0"),
            sa.Column("telegram_bot_token", sa.String(length=256), nullable=False, server_default=""),
            sa.Column("telegram_chat_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_monitor_menus_owner_user_id", "monitor_menus", ["owner_user_id"], unique=False)

    if not _has_table("wallet_watches"):
        op.create_table(
            "wallet_watches",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("owner_user_id", sa.Integer(), nullable=False),
            sa.Column("monitor_menu_id", sa.Integer(), nullable=False),
            sa.Column("address", sa.String(length=128), nullable=False),
            sa.Column("alias", sa.String(length=128), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_wallet_watches_address", "wallet_watches", ["address"], unique=False)
        op.create_index("ix_wallet_watches_monitor_menu_id", "wallet_watches", ["monitor_menu_id"], unique=False)
        op.create_index("ix_wallet_watches_owner_user_id", "wallet_watches", ["owner_user_id"], unique=False)
        op.create_index("uq_wallet_menu_address", "wallet_watches", ["monitor_menu_id", "address"], unique=True)

    if not _has_table("chain_events"):
        op.create_table(
            "chain_events",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("owner_user_id", sa.Integer(), nullable=False),
            sa.Column("monitor_menu_id", sa.Integer(), nullable=False),
            sa.Column("block_number", sa.Integer(), nullable=False),
            sa.Column("event_index", sa.Integer(), nullable=False),
            sa.Column("extrinsic_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("pallet", sa.String(length=64), nullable=False),
            sa.Column("event_name", sa.String(length=64), nullable=False),
            sa.Column("action_type", sa.String(length=64), nullable=False, server_default="generic_call"),
            sa.Column("call_name", sa.String(length=96), nullable=False, server_default=""),
            sa.Column("amount_tao", sa.Float(), nullable=False, server_default="0"),
            sa.Column("from_address", sa.String(length=128), nullable=True),
            sa.Column("to_address", sa.String(length=128), nullable=True),
            sa.Column("signer_address", sa.String(length=128), nullable=True),
            sa.Column("extrinsic_hash", sa.String(length=128), nullable=True),
            sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("failure_reason", sa.Text(), nullable=True),
            sa.Column("involved_addresses_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("matched_aliases_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("raw_payload", sa.Text(), nullable=False),
            sa.Column("notification_sent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("detected_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_chain_events_block_number", "chain_events", ["block_number"], unique=False)
        op.create_index("ix_chain_events_detected_at", "chain_events", ["detected_at"], unique=False)
        op.create_index("ix_chain_events_from_address", "chain_events", ["from_address"], unique=False)
        op.create_index("ix_chain_events_monitor_menu_id", "chain_events", ["monitor_menu_id"], unique=False)
        op.create_index("ix_chain_events_owner_user_id", "chain_events", ["owner_user_id"], unique=False)
        op.create_index("ix_chain_events_signer_address", "chain_events", ["signer_address"], unique=False)
        op.create_index("ix_chain_events_to_address", "chain_events", ["to_address"], unique=False)
        op.create_index("uq_menu_block_event", "chain_events", ["monitor_menu_id", "block_number", "event_index"], unique=True)

    if not _has_table("monitor_state"):
        op.create_table(
            "monitor_state",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("last_scanned_block", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_seen_head", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("monitor_status", sa.String(length=32), nullable=False, server_default="idle"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _has_table("app_settings"):
        op.create_table(
            "app_settings",
            sa.Column("key", sa.String(length=128), nullable=False),
            sa.Column("value", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("key"),
        )

    if not _has_table("user_settings"):
        op.create_table(
            "user_settings",
            sa.Column("owner_user_id", sa.Integer(), nullable=False),
            sa.Column("large_transfer_threshold_tao", sa.Float(), nullable=False, server_default="5"),
            sa.Column("telegram_bot_token", sa.String(length=256), nullable=False, server_default=""),
            sa.Column("telegram_chat_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("owner_user_id"),
        )

    if not _has_table("notification_outbox"):
        op.create_table(
            "notification_outbox",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("owner_user_id", sa.Integer(), nullable=False),
            sa.Column("monitor_menu_id", sa.Integer(), nullable=False),
            sa.Column("chain_event_id", sa.Integer(), nullable=True),
            sa.Column("telegram_bot_token", sa.String(length=256), nullable=False),
            sa.Column("telegram_chat_id", sa.String(length=128), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("next_retry_at", sa.DateTime(), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_notification_outbox_chain_event_id", "notification_outbox", ["chain_event_id"], unique=False)
        op.create_index("ix_notification_outbox_monitor_menu_id", "notification_outbox", ["monitor_menu_id"], unique=False)
        op.create_index("ix_notification_outbox_next_retry_at", "notification_outbox", ["next_retry_at"], unique=False)
        op.create_index("ix_notification_outbox_owner_user_id", "notification_outbox", ["owner_user_id"], unique=False)
        op.create_index("ix_notification_outbox_status_next_retry", "notification_outbox", ["status", "next_retry_at"], unique=False)


def downgrade() -> None:
    # 生产数据不建议自动降级删除表，保留空实现避免误操作造成资料丢失。
    pass

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# 监控菜单表：每个账号可拥有多个监控菜单，每个菜单都有自己的 TG 和阈值配置。
class MonitorMenu(Base):
    __tablename__ = "monitor_menus"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(128))
    menu_kind: Mapped[str] = mapped_column(String(32), default="wallet")
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    large_transfer_threshold_tao: Mapped[float] = mapped_column(Float, default=0.0)
    telegram_bot_token: Mapped[str] = mapped_column(String(256), default="")
    telegram_chat_id: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# 钱包监控表：每个后台账号下的每个监控菜单都有自己独立的钱包列表。
class WalletWatch(Base):
    __tablename__ = "wallet_watches"
    __table_args__ = (Index("uq_wallet_menu_address", "monitor_menu_id", "address", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, index=True)
    monitor_menu_id: Mapped[int] = mapped_column(Integer, index=True)
    address: Mapped[str] = mapped_column(String(128), index=True)
    alias: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# 链上命中事件表：同一笔链上事件可以按监控菜单分别入库，互不干扰。
class ChainEvent(Base):
    __tablename__ = "chain_events"
    __table_args__ = (Index("uq_menu_block_event", "monitor_menu_id", "block_number", "event_index", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, index=True)
    monitor_menu_id: Mapped[int] = mapped_column(Integer, index=True)
    block_number: Mapped[int] = mapped_column(Integer, index=True)
    event_index: Mapped[int] = mapped_column(Integer)
    extrinsic_index: Mapped[int] = mapped_column(Integer, default=0)
    pallet: Mapped[str] = mapped_column(String(64))
    event_name: Mapped[str] = mapped_column(String(64))
    action_type: Mapped[str] = mapped_column(String(64), default="generic_call")
    call_name: Mapped[str] = mapped_column(String(96), default="")
    amount_tao: Mapped[float] = mapped_column(Float, default=0.0)
    from_address: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    to_address: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    signer_address: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    extrinsic_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    involved_addresses_json: Mapped[str] = mapped_column(Text, default="[]")
    matched_aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    message: Mapped[str] = mapped_column(Text)
    raw_payload: Mapped[str] = mapped_column(Text)
    notification_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


# 监听状态表：只保存一行，用于记录扫描进度和错误信息。
class MonitorState(Base):
    __tablename__ = "monitor_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_scanned_block: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_head: Mapped[int] = mapped_column(Integer, default=0)
    monitor_status: Mapped[str] = mapped_column(String(32), default="idle")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# 动态设置表：网页里改过的配置会存进这里。
class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


# 用户通知设置表：Telegram 和阈值改成每个账号独立配置。
class UserSetting(Base):
    __tablename__ = "user_settings"

    owner_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    large_transfer_threshold_tao: Mapped[float] = mapped_column(Float, default=5.0)
    telegram_bot_token: Mapped[str] = mapped_column(String(256), default="")
    telegram_chat_id: Mapped[str] = mapped_column(String(128), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# 后台账号表：总管理员和普通账号都存这里，总管理员还可查看可回显密码。
class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    password_ciphertext: Mapped[str] = mapped_column(Text, default="")
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# Telegram 通知队列表：链上扫描只负责写入队列，独立后台任务负责发送和重试。
class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        Index("ix_notification_outbox_status_next_retry", "status", "next_retry_at"),
        Index("ix_notification_outbox_chain_event_id", "chain_event_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, index=True)
    monitor_menu_id: Mapped[int] = mapped_column(Integer, index=True)
    chain_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    telegram_bot_token: Mapped[str] = mapped_column(String(256))
    telegram_chat_id: Mapped[str] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=10)
    next_retry_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

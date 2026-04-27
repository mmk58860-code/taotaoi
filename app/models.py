from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# 钱包监控表：每个后台账号都有自己独立的钱包监控列表。
class WalletWatch(Base):
    __tablename__ = "wallet_watches"
    __table_args__ = (Index("uq_wallet_owner_address", "owner_user_id", "address", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, index=True)
    address: Mapped[str] = mapped_column(String(128), index=True)
    alias: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# 链上命中事件表：同一笔链上事件可以按账号分别入库，互不干扰。
class ChainEvent(Base):
    __tablename__ = "chain_events"
    __table_args__ = (Index("uq_owner_block_event", "owner_user_id", "block_number", "event_index", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(Integer, index=True)
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

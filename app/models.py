from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# 钱包监控表：保存需要跟踪的钱包地址和别名。
class WalletWatch(Base):
    __tablename__ = "wallet_watches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    alias: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# 链上命中事件表：保存被监控到的交易事件。
class ChainEvent(Base):
    __tablename__ = "chain_events"
    __table_args__ = (UniqueConstraint("block_number", "event_index", name="uq_block_event"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    block_number: Mapped[int] = mapped_column(Integer, index=True)
    event_index: Mapped[int] = mapped_column(Integer)
    pallet: Mapped[str] = mapped_column(String(64))
    event_name: Mapped[str] = mapped_column(String(64))
    amount_tao: Mapped[float] = mapped_column(Float, default=0.0)
    from_address: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    to_address: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    extrinsic_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
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


# 后台账号表：总管理员和普通账号都存这里。
class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

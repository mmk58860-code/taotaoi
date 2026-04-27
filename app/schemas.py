from datetime import datetime

from pydantic import BaseModel, Field


# 新增钱包时的表单结构。
class WalletCreate(BaseModel):
    address: str = Field(min_length=3, max_length=128)
    alias: str = Field(min_length=1, max_length=128)


# 总管理员保存系统链路设置时的表单结构。
class SystemSettingsUpdate(BaseModel):
    subtensor_ws_url: str
    network_name: str
    poll_interval_seconds: int = Field(ge=2, le=120)
    finality_lag_blocks: int = Field(ge=0, le=20)


# 每个账号保存自己通知配置时的表单结构。
class UserNotificationSettingsUpdate(BaseModel):
    large_transfer_threshold_tao: float = Field(ge=0)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


# 返回给模板或接口的钱包结构。
class WalletOut(BaseModel):
    id: int
    address: str
    alias: str
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# 返回给接口的事件结构。
class ChainEventOut(BaseModel):
    id: int
    block_number: int
    pallet: str
    event_name: str
    amount_tao: float
    from_address: str | None
    to_address: str | None
    message: str
    detected_at: datetime

    model_config = {"from_attributes": True}

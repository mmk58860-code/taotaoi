from datetime import datetime

from pydantic import BaseModel, Field


class WalletCreate(BaseModel):
    address: str = Field(min_length=3, max_length=128)
    alias: str = Field(min_length=1, max_length=128)


class SettingsUpdate(BaseModel):
    subtensor_ws_url: str
    network_name: str
    large_transfer_threshold_tao: float = Field(ge=0)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    poll_interval_seconds: int = Field(ge=2, le=120)
    finality_lag_blocks: int = Field(ge=0, le=20)


class WalletOut(BaseModel):
    id: int
    address: str
    alias: str
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


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


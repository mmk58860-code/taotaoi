from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppSetting
from app.schemas import SettingsUpdate


SETTING_KEYS = (
    "subtensor_ws_url",
    "network_name",
    "large_transfer_threshold_tao",
    "telegram_bot_token",
    "telegram_chat_id",
    "poll_interval_seconds",
    "finality_lag_blocks",
)


def bootstrap_settings(session: Session) -> None:
    defaults = get_default_settings()
    existing = {row.key for row in session.scalars(select(AppSetting)).all()}
    for key, value in defaults.items():
        if key not in existing:
            session.add(AppSetting(key=key, value=str(value)))


def get_default_settings() -> dict[str, str | float | int]:
    settings = get_settings()
    return {
        "subtensor_ws_url": settings.subtensor_ws_url,
        "network_name": settings.network_name,
        "large_transfer_threshold_tao": settings.large_transfer_threshold_tao,
        "telegram_bot_token": settings.telegram_bot_token,
        "telegram_chat_id": settings.telegram_chat_id,
        "poll_interval_seconds": settings.poll_interval_seconds,
        "finality_lag_blocks": settings.finality_lag_blocks,
    }


def get_runtime_settings(session: Session) -> dict[str, str]:
    bootstrap_settings(session)
    rows = session.scalars(select(AppSetting)).all()
    return {row.key: row.value for row in rows}


def update_runtime_settings(session: Session, payload: SettingsUpdate) -> dict[str, str]:
    values = payload.model_dump()
    for key, raw_value in values.items():
        row = session.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=str(raw_value))
            session.add(row)
        else:
            row.value = str(raw_value)
    session.flush()
    return get_runtime_settings(session)


def typed_runtime_settings(raw: Mapping[str, str]) -> dict[str, str | float | int]:
    return {
        "subtensor_ws_url": raw.get("subtensor_ws_url", ""),
        "network_name": raw.get("network_name", "finney"),
        "large_transfer_threshold_tao": float(raw.get("large_transfer_threshold_tao", "5")),
        "telegram_bot_token": raw.get("telegram_bot_token", ""),
        "telegram_chat_id": raw.get("telegram_chat_id", ""),
        "poll_interval_seconds": int(raw.get("poll_interval_seconds", "6")),
        "finality_lag_blocks": int(raw.get("finality_lag_blocks", "1")),
    }


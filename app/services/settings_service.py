from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppSetting, UserSetting
from app.schemas import SystemSettingsUpdate, UserNotificationSettingsUpdate


SYSTEM_SETTING_KEYS = (
    "subtensor_ws_url",
    "network_name",
    "poll_interval_seconds",
    "finality_lag_blocks",
)


def bootstrap_system_settings(session: Session) -> None:
    defaults = get_system_default_settings()
    existing = {row.key for row in session.scalars(select(AppSetting)).all()}
    for key, value in defaults.items():
        if key not in existing:
            session.add(AppSetting(key=key, value=str(value)))


def bootstrap_user_settings(session: Session, owner_user_id: int) -> UserSetting:
    # 每个账号第一次出现时都生成自己的 TG 和阈值配置行。
    row = session.get(UserSetting, owner_user_id)
    if row is None:
        defaults = get_user_default_settings()
        row = UserSetting(
            owner_user_id=owner_user_id,
            large_transfer_threshold_tao=float(defaults["large_transfer_threshold_tao"]),
            telegram_bot_token=str(defaults["telegram_bot_token"]),
            telegram_chat_id=str(defaults["telegram_chat_id"]),
        )
        session.add(row)
        session.flush()
    return row


def get_system_default_settings() -> dict[str, str | int]:
    settings = get_settings()
    return {
        "subtensor_ws_url": settings.subtensor_ws_url,
        "network_name": settings.network_name,
        "poll_interval_seconds": settings.poll_interval_seconds,
        "finality_lag_blocks": settings.finality_lag_blocks,
    }


def get_user_default_settings() -> dict[str, str | float]:
    return {
        "large_transfer_threshold_tao": 5.0,
        "telegram_bot_token": "",
        "telegram_chat_id": "",
    }


def get_system_runtime_settings(session: Session) -> dict[str, str]:
    bootstrap_system_settings(session)
    rows = session.scalars(select(AppSetting)).all()
    return {row.key: row.value for row in rows if row.key in SYSTEM_SETTING_KEYS}


def update_system_runtime_settings(session: Session, payload: SystemSettingsUpdate) -> dict[str, str]:
    values = payload.model_dump()
    for key, raw_value in values.items():
        row = session.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=str(raw_value))
            session.add(row)
        else:
            row.value = str(raw_value)
    session.flush()
    return get_system_runtime_settings(session)


def get_user_runtime_settings(session: Session, owner_user_id: int) -> dict[str, str | float]:
    row = bootstrap_user_settings(session, owner_user_id)
    return {
        "large_transfer_threshold_tao": float(row.large_transfer_threshold_tao),
        "telegram_bot_token": row.telegram_bot_token,
        "telegram_chat_id": row.telegram_chat_id,
    }


def update_user_runtime_settings(
    session: Session,
    owner_user_id: int,
    payload: UserNotificationSettingsUpdate,
) -> dict[str, str | float]:
    row = bootstrap_user_settings(session, owner_user_id)
    row.large_transfer_threshold_tao = payload.large_transfer_threshold_tao
    row.telegram_bot_token = payload.telegram_bot_token
    row.telegram_chat_id = payload.telegram_chat_id
    session.flush()
    return get_user_runtime_settings(session, owner_user_id)


def migrate_legacy_user_settings(session: Session, owner_user_id: int) -> None:
    # 兼容旧版单账号配置：把原来的全局 TG 和阈值搬到总管理员个人设置里。
    settings = get_settings()
    row = bootstrap_user_settings(session, owner_user_id)
    legacy_rows = session.scalars(
        select(AppSetting).where(
            AppSetting.key.in_(("large_transfer_threshold_tao", "telegram_bot_token", "telegram_chat_id"))
        )
    ).all()
    legacy_map = {item.key: item.value for item in legacy_rows}

    if (
        float(row.large_transfer_threshold_tao) == float(get_user_default_settings()["large_transfer_threshold_tao"])
        and legacy_map.get("large_transfer_threshold_tao")
    ):
        row.large_transfer_threshold_tao = float(legacy_map["large_transfer_threshold_tao"])
    elif float(row.large_transfer_threshold_tao) == float(get_user_default_settings()["large_transfer_threshold_tao"]):
        row.large_transfer_threshold_tao = float(settings.large_transfer_threshold_tao)
    if not row.telegram_bot_token and legacy_map.get("telegram_bot_token"):
        row.telegram_bot_token = legacy_map["telegram_bot_token"]
    elif not row.telegram_bot_token and settings.telegram_bot_token:
        row.telegram_bot_token = settings.telegram_bot_token
    if not row.telegram_chat_id and legacy_map.get("telegram_chat_id"):
        row.telegram_chat_id = legacy_map["telegram_chat_id"]
    elif not row.telegram_chat_id and settings.telegram_chat_id:
        row.telegram_chat_id = settings.telegram_chat_id
    session.flush()


def typed_system_runtime_settings(raw: Mapping[str, str]) -> dict[str, str | int]:
    return {
        "subtensor_ws_url": raw.get("subtensor_ws_url", ""),
        "network_name": raw.get("network_name", "finney"),
        "poll_interval_seconds": int(raw.get("poll_interval_seconds", "1")),
        "finality_lag_blocks": int(raw.get("finality_lag_blocks", "0")),
    }

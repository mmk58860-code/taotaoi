from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import MonitorMenu, UserSetting
from app.schemas import MonitorMenuCreate, MonitorMenuRename, MonitorMenuSettingsUpdate


BUILTIN_ALERT_KIND = "alert"
BUILTIN_WALLET_KIND = "wallet"


def bootstrap_monitor_menus(session: Session, owner_user_id: int) -> list[MonitorMenu]:
    # 每个账号至少拥有两个基础菜单：大额预警、钱包监控。
    _ensure_builtin_menu(
        session=session,
        owner_user_id=owner_user_id,
        menu_kind=BUILTIN_ALERT_KIND,
        default_name="大额预警",
        sort_order=20,
        threshold_tao=5.0,
    )
    _ensure_builtin_menu(
        session=session,
        owner_user_id=owner_user_id,
        menu_kind=BUILTIN_WALLET_KIND,
        default_name="钱包监控",
        sort_order=30,
        threshold_tao=0.0,
    )
    return list_monitor_menus(session, owner_user_id)


def list_monitor_menus(session: Session, owner_user_id: int) -> list[MonitorMenu]:
    bootstrap_monitor_menus(session, owner_user_id)
    return session.scalars(
        select(MonitorMenu)
        .where(MonitorMenu.owner_user_id == owner_user_id)
        .order_by(MonitorMenu.sort_order.asc(), MonitorMenu.created_at.asc(), MonitorMenu.id.asc())
    ).all()


def get_monitor_menu(session: Session, owner_user_id: int, menu_id: int) -> MonitorMenu | None:
    row = session.get(MonitorMenu, menu_id)
    if row is None or row.owner_user_id != owner_user_id:
        return None
    return row


def get_builtin_menu(session: Session, owner_user_id: int, menu_kind: str) -> MonitorMenu | None:
    bootstrap_monitor_menus(session, owner_user_id)
    return session.scalar(
        select(MonitorMenu).where(
            MonitorMenu.owner_user_id == owner_user_id,
            MonitorMenu.menu_kind == menu_kind,
            MonitorMenu.is_builtin.is_(True),
        )
    )


def create_custom_wallet_menu(session: Session, owner_user_id: int, payload: MonitorMenuCreate) -> MonitorMenu:
    # 自定义菜单本质上是另一组独立的钱包监控空间。
    current_max = session.scalar(
        select(func.max(MonitorMenu.sort_order)).where(MonitorMenu.owner_user_id == owner_user_id)
    )
    row = MonitorMenu(
        owner_user_id=owner_user_id,
        name=payload.name.strip(),
        menu_kind=BUILTIN_WALLET_KIND,
        is_builtin=False,
        sort_order=int(current_max or 30) + 10,
        large_transfer_threshold_tao=0.0,
        telegram_bot_token="",
        telegram_chat_id="",
    )
    session.add(row)
    session.flush()
    return row


def rename_monitor_menu(session: Session, owner_user_id: int, menu_id: int, payload: MonitorMenuRename) -> MonitorMenu | None:
    row = get_monitor_menu(session, owner_user_id, menu_id)
    if row is None:
        return None
    row.name = payload.name.strip()
    session.flush()
    return row


def get_menu_runtime_settings(session: Session, owner_user_id: int, menu_id: int) -> dict[str, str | float]:
    row = get_monitor_menu(session, owner_user_id, menu_id)
    if row is None:
        return {}
    return {
        "telegram_bot_token": row.telegram_bot_token,
        "telegram_chat_id": row.telegram_chat_id,
        "large_transfer_threshold_tao": float(row.large_transfer_threshold_tao),
        "menu_kind": row.menu_kind,
        "name": row.name,
        "is_builtin": row.is_builtin,
    }


def update_menu_runtime_settings(
    session: Session,
    owner_user_id: int,
    menu_id: int,
    payload: MonitorMenuSettingsUpdate,
) -> dict[str, str | float]:
    row = get_monitor_menu(session, owner_user_id, menu_id)
    if row is None:
        return {}
    row.telegram_bot_token = payload.telegram_bot_token
    row.telegram_chat_id = payload.telegram_chat_id
    if row.menu_kind == BUILTIN_ALERT_KIND:
        row.large_transfer_threshold_tao = payload.large_transfer_threshold_tao
    session.flush()
    return get_menu_runtime_settings(session, owner_user_id, menu_id)


def migrate_legacy_user_settings_to_menus(session: Session, owner_user_id: int) -> None:
    # 老版本只有一份用户设置，这里迁到“大额预警”和“钱包监控”两个基础菜单上。
    legacy = session.get(UserSetting, owner_user_id)
    if legacy is None:
        return

    alert_menu = get_builtin_menu(session, owner_user_id, BUILTIN_ALERT_KIND)
    wallet_menu = get_builtin_menu(session, owner_user_id, BUILTIN_WALLET_KIND)
    if alert_menu is None or wallet_menu is None:
        return

    if alert_menu.large_transfer_threshold_tao in (0.0, 5.0):
        alert_menu.large_transfer_threshold_tao = float(legacy.large_transfer_threshold_tao)
    if not alert_menu.telegram_bot_token and legacy.telegram_bot_token:
        alert_menu.telegram_bot_token = legacy.telegram_bot_token
    if not alert_menu.telegram_chat_id and legacy.telegram_chat_id:
        alert_menu.telegram_chat_id = legacy.telegram_chat_id

    if not wallet_menu.telegram_bot_token and legacy.telegram_bot_token:
        wallet_menu.telegram_bot_token = legacy.telegram_bot_token
    if not wallet_menu.telegram_chat_id and legacy.telegram_chat_id:
        wallet_menu.telegram_chat_id = legacy.telegram_chat_id
    session.flush()


def _ensure_builtin_menu(
    session: Session,
    owner_user_id: int,
    menu_kind: str,
    default_name: str,
    sort_order: int,
    threshold_tao: float,
) -> MonitorMenu:
    row = session.scalar(
        select(MonitorMenu).where(
            MonitorMenu.owner_user_id == owner_user_id,
            MonitorMenu.menu_kind == menu_kind,
            MonitorMenu.is_builtin.is_(True),
        )
    )
    if row is None:
        row = MonitorMenu(
            owner_user_id=owner_user_id,
            name=default_name,
            menu_kind=menu_kind,
            is_builtin=True,
            sort_order=sort_order,
            large_transfer_threshold_tao=threshold_tao,
            telegram_bot_token="",
            telegram_chat_id="",
        )
        session.add(row)
        session.flush()
    return row

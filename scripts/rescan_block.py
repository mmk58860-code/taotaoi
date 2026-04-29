from __future__ import annotations

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import session_scope
from app.models import MonitorMenu, WalletWatch
from app.services.settings_service import get_system_runtime_settings, typed_system_runtime_settings
from app.services.subtensor_monitor import NotificationProfile, SubtensorMonitor
from sqlalchemy import select


def load_runtime_context() -> tuple[str, dict[str, dict[int, list[str]]], dict[int, NotificationProfile]]:
    with session_scope() as session:
        raw_settings = get_system_runtime_settings(session)
        typed = typed_system_runtime_settings(raw_settings)
        menu_rows = session.scalars(select(MonitorMenu).order_by(MonitorMenu.sort_order.asc(), MonitorMenu.id.asc())).all()
        wallet_rows = session.scalars(select(WalletWatch).where(WalletWatch.enabled.is_(True))).all()

    monitor = SubtensorMonitor()
    watch_map = monitor._build_watch_map(wallet_rows)
    profile_map = {
        row.id: NotificationProfile(
            monitor_menu_id=row.id,
            owner_user_id=row.owner_user_id,
            menu_kind=row.menu_kind,
            menu_name=row.name,
            threshold_tao=float(row.large_transfer_threshold_tao or 0),
            telegram_bot_token=row.telegram_bot_token or "",
            telegram_chat_id=row.telegram_chat_id or "",
        )
        for row in menu_rows
    }
    return str(typed["subtensor_ws_url"]), watch_map, profile_map


async def main() -> int:
    if len(sys.argv) != 2:
        print("用法: .venv/bin/python scripts/rescan_block.py <区块号>")
        return 1

    block_number = int(sys.argv[1])
    ws_url, watch_map, profile_map = load_runtime_context()

    monitor = SubtensorMonitor()
    substrate = monitor._get_substrate(ws_url)
    actions = await asyncio.to_thread(
        monitor._extract_actions_sync,
        substrate,
        block_number,
        watch_map,
        profile_map,
    )
    for action in actions:
        action.should_notify = False

    await monitor._persist_and_notify(actions)
    monitor._close_substrate()
    print(f"已重扫区块 {block_number}，共处理 {len(actions)} 条动作")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

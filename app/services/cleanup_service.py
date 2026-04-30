from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete

from app.config import get_settings
from app.database import session_scope
from app.models import ChainEvent


logger = logging.getLogger(__name__)
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class CleanupService:
    def __init__(self) -> None:
        # 清理任务只处理命中历史，不删除钱包、菜单、TG 和系统设置。
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="chain-event-cleanup")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            settings = get_settings()
            if not settings.cleanup_enabled:
                await self._wait_seconds(3600)
                continue

            try:
                deleted = await asyncio.to_thread(self.cleanup_once)
                logger.info("历史命中清理完成，删除 %s 条过期数据", deleted)
            except Exception:
                logger.exception("历史命中清理失败，稍后会继续尝试")

            interval_seconds = max(60, int(settings.cleanup_interval_minutes) * 60)
            await self._wait_seconds(interval_seconds)

    def cleanup_once(self) -> int:
        settings = get_settings()
        retention_hours = int(getattr(settings, "cleanup_retention_hours", 0) or 0)
        if retention_hours > 0:
            cutoff_beijing = datetime.now(BEIJING_TZ) - timedelta(hours=retention_hours)
        else:
            retention_days = max(1, int(settings.cleanup_retention_days))
            cutoff_beijing = datetime.now(BEIJING_TZ) - timedelta(days=retention_days)
        cutoff_utc_naive = cutoff_beijing.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        with session_scope() as session:
            result = session.execute(delete(ChainEvent).where(ChainEvent.detected_at < cutoff_utc_naive))
            return int(result.rowcount or 0)

    async def _wait_seconds(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

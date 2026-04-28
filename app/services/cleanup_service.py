from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, time, timedelta
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

            wait_seconds = self._seconds_until_next_run(settings.cleanup_time)
            await self._wait_seconds(wait_seconds)
            if self._stop_event.is_set():
                break

            try:
                deleted = await asyncio.to_thread(self.cleanup_once)
                logger.info("历史命中清理完成，删除 %s 条 3 天前数据", deleted)
            except Exception:
                logger.exception("历史命中清理失败，下一天会继续尝试")

    def cleanup_once(self) -> int:
        settings = get_settings()
        retention_days = max(1, int(settings.cleanup_retention_days))
        cutoff_beijing = datetime.now(BEIJING_TZ) - timedelta(days=retention_days)
        cutoff_utc_naive = cutoff_beijing.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        with session_scope() as session:
            result = session.execute(delete(ChainEvent).where(ChainEvent.detected_at < cutoff_utc_naive))
            return int(result.rowcount or 0)

    def _seconds_until_next_run(self, cleanup_time: str) -> float:
        hour, minute = self._parse_cleanup_time(cleanup_time)
        now = datetime.now(BEIJING_TZ)
        next_run = datetime.combine(now.date(), time(hour=hour, minute=minute), tzinfo=BEIJING_TZ)
        if next_run <= now:
            next_run += timedelta(days=1)
        return max(1.0, (next_run - now).total_seconds())

    def _parse_cleanup_time(self, cleanup_time: str) -> tuple[int, int]:
        try:
            hour_text, minute_text = cleanup_time.strip().split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except Exception:
            pass
        logger.warning("CLEANUP_TIME 配置无效：%s，已退回 04:00", cleanup_time)
        return 4, 0

    async def _wait_seconds(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

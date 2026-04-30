from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta

from sqlalchemy import select

from app.database import session_scope
from app.models import ChainEvent, NotificationOutbox
from app.services.telegram import TelegramNotifier


logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self) -> None:
        # TG 发送独立于链上扫描，避免 Telegram 网络抖动拖慢区块处理。
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._notifier = TelegramNotifier()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="telegram-notification-worker")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                sent_count = await self.process_once()
                wait_seconds = 1 if sent_count else 3
            except Exception:
                logger.exception("Telegram 通知队列处理失败，稍后重试")
                wait_seconds = 5
            await self._wait_seconds(wait_seconds)

    async def process_once(self, limit: int = 20) -> int:
        now = datetime.utcnow()
        with session_scope() as session:
            stale_rows = session.scalars(
                select(NotificationOutbox).where(
                    NotificationOutbox.status == "sending",
                    NotificationOutbox.updated_at <= now - timedelta(minutes=2),
                )
            ).all()
            for stale in stale_rows:
                stale.status = "retrying"
                stale.next_retry_at = now
                stale.updated_at = now

            rows = session.scalars(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.status.in_(("pending", "retrying")),
                    NotificationOutbox.next_retry_at <= now,
                )
                .order_by(NotificationOutbox.next_retry_at.asc(), NotificationOutbox.id.asc())
                .limit(limit)
            ).all()
            for row in rows:
                row.status = "sending"
                row.updated_at = now

        sent_count = 0
        for row in rows:
            if await self._send_one(row.id):
                sent_count += 1
        return sent_count

    async def _send_one(self, outbox_id: int) -> bool:
        with session_scope() as session:
            row = session.get(NotificationOutbox, outbox_id)
            if row is None or row.status != "sending":
                return False
            token = row.telegram_bot_token
            chat_id = row.telegram_chat_id
            message = row.message

        try:
            sent = await self._notifier.send_message(token=token, chat_id=chat_id, text=message)
            if not sent:
                raise RuntimeError("Telegram API 返回发送失败")
        except Exception as exc:
            self._mark_failed(outbox_id, str(exc))
            return False

        with session_scope() as session:
            row = session.get(NotificationOutbox, outbox_id)
            if row is None:
                return True
            row.status = "sent"
            row.sent_at = datetime.utcnow()
            row.updated_at = row.sent_at
            row.last_error = ""
            if row.chain_event_id:
                event = session.get(ChainEvent, row.chain_event_id)
                if event is not None:
                    event.notification_sent = True
        return True

    def _mark_failed(self, outbox_id: int, error_text: str) -> None:
        with session_scope() as session:
            row = session.get(NotificationOutbox, outbox_id)
            if row is None:
                return
            row.attempts += 1
            row.last_error = error_text[:2000]
            row.updated_at = datetime.utcnow()
            if row.attempts >= row.max_attempts:
                row.status = "failed"
                return
            delay_seconds = min(300, 2 ** min(row.attempts, 8))
            row.status = "retrying"
            row.next_retry_at = datetime.utcnow() + timedelta(seconds=delay_seconds)

    async def _wait_seconds(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

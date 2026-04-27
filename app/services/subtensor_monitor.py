from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from substrateinterface import SubstrateInterface

from app.database import session_scope
from app.models import ChainEvent, MonitorState, WalletWatch
from app.services.settings_service import get_runtime_settings, typed_runtime_settings
from app.services.telegram import TelegramNotifier


logger = logging.getLogger(__name__)
RAO_PER_TAO = 1_000_000_000


@dataclass
class TransferRecord:
    block_number: int
    event_index: int
    pallet: str
    event_name: str
    amount_tao: float
    from_address: str | None
    to_address: str | None
    extrinsic_hash: str | None
    message: str
    raw_payload: str
    should_notify: bool


class SubtensorMonitor:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._wakeup_event = asyncio.Event()
        self._notifier = TelegramNotifier()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="subtensor-monitor")

    async def stop(self) -> None:
        self._stop_event.set()
        self._wakeup_event.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def restart(self) -> None:
        self._wakeup_event.set()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._scan_once()
            except Exception as exc:
                logger.exception("monitor loop failed")
                with session_scope() as session:
                    state = ensure_state(session)
                    state.monitor_status = "error"
                    state.last_error = str(exc)
                    state.updated_at = datetime.utcnow()
                await asyncio.sleep(10)
                continue

            wait_seconds = self._current_poll_interval()
            try:
                await asyncio.wait_for(self._wakeup_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
            self._wakeup_event.clear()

    def _current_poll_interval(self) -> int:
        with session_scope() as session:
            raw = get_runtime_settings(session)
        settings = typed_runtime_settings(raw)
        return int(settings["poll_interval_seconds"])

    async def _scan_once(self) -> None:
        with session_scope() as session:
            raw_settings = get_runtime_settings(session)
            typed = typed_runtime_settings(raw_settings)
            state = ensure_state(session)
            wallet_rows = session.scalars(select(WalletWatch).where(WalletWatch.enabled.is_(True))).all()
            watch_map = {row.address: row.alias for row in wallet_rows}
            state.monitor_status = "running"
            state.last_error = None

        substrate = SubstrateInterface(url=str(typed["subtensor_ws_url"]))
        latest_block = int(substrate.get_block_number(substrate.get_chain_finalised_head()))
        target_block = max(0, latest_block - int(typed["finality_lag_blocks"]))

        with session_scope() as session:
            state = ensure_state(session)
            start_block = state.last_scanned_block + 1 if state.last_scanned_block else max(target_block - 20, 1)
            state.last_seen_head = latest_block

        if start_block > target_block:
            return

        for block_number in range(start_block, target_block + 1):
            transfers = self._extract_transfers(
                substrate=substrate,
                block_number=block_number,
                threshold_tao=float(typed["large_transfer_threshold_tao"]),
                watch_map=watch_map,
            )
            await self._persist_and_notify(transfers, typed)
            with session_scope() as session:
                state = ensure_state(session)
                state.last_scanned_block = block_number
                state.updated_at = datetime.utcnow()

    def _extract_transfers(
        self,
        substrate: SubstrateInterface,
        block_number: int,
        threshold_tao: float,
        watch_map: dict[str, str],
    ) -> list[TransferRecord]:
        block_hash = substrate.get_block_hash(block_number)
        events = substrate.get_events(block_hash=block_hash)
        block = substrate.get_block(block_hash=block_hash)
        extrinsics = block.get("extrinsics", []) if isinstance(block, dict) else []
        results: list[TransferRecord] = []

        for index, event in enumerate(events):
            event_value = getattr(event, "value", event)
            event_meta = self._to_dict(event_value)
            pallet = self._pick_string(event_meta, ("module_id", "pallet", "module"))
            event_name = self._pick_string(event_meta, ("event_id", "event", "name"))
            if pallet.lower() != "balances" or event_name.lower() != "transfer":
                continue

            attributes = event_meta.get("attributes", [])
            from_address, to_address, amount_rao = self._parse_transfer_attributes(attributes)
            if amount_rao is None:
                continue

            amount_tao = round(amount_rao / RAO_PER_TAO, 9)
            watch_aliases = [alias for address, alias in watch_map.items() if address in {from_address, to_address}]
            watched = bool(watch_aliases)
            above_threshold = amount_tao >= threshold_tao
            if not watched and not above_threshold:
                continue

            extrinsic_hash = self._resolve_extrinsic_hash(extrinsics, event_meta)
            tags = []
            if watched:
                tags.append(f"wallet: {', '.join(watch_aliases)}")
            if above_threshold:
                tags.append(f"threshold: >= {threshold_tao} TAO")
            message = (
                f"<b>{pallet}.{event_name}</b>\n"
                f"Block: <code>{block_number}</code>\n"
                f"Amount: <b>{amount_tao:.6f} TAO</b>\n"
                f"From: <code>{from_address or '-'}</code>\n"
                f"To: <code>{to_address or '-'}</code>\n"
                f"Reason: {', '.join(tags)}"
            )
            results.append(
                TransferRecord(
                    block_number=block_number,
                    event_index=index,
                    pallet=pallet,
                    event_name=event_name,
                    amount_tao=amount_tao,
                    from_address=from_address,
                    to_address=to_address,
                    extrinsic_hash=extrinsic_hash,
                    message=message,
                    raw_payload=json.dumps(event_meta, ensure_ascii=True, default=str),
                    should_notify=True,
                )
            )

        return results

    async def _persist_and_notify(self, transfers: list[TransferRecord], settings: dict[str, str | float | int]) -> None:
        if not transfers:
            return
        for transfer in transfers:
            with session_scope() as session:
                exists = session.scalar(
                    select(ChainEvent).where(
                        ChainEvent.block_number == transfer.block_number,
                        ChainEvent.event_index == transfer.event_index,
                    )
                )
                if exists:
                    continue
                row = ChainEvent(
                    block_number=transfer.block_number,
                    event_index=transfer.event_index,
                    pallet=transfer.pallet,
                    event_name=transfer.event_name,
                    amount_tao=transfer.amount_tao,
                    from_address=transfer.from_address,
                    to_address=transfer.to_address,
                    extrinsic_hash=transfer.extrinsic_hash,
                    message=transfer.message,
                    raw_payload=transfer.raw_payload,
                    notification_sent=False,
                )
                session.add(row)
                session.flush()
                should_send = transfer.should_notify and bool(settings["telegram_bot_token"]) and bool(settings["telegram_chat_id"])
            if should_send:
                sent = await self._notifier.send_message(
                    token=str(settings["telegram_bot_token"]),
                    chat_id=str(settings["telegram_chat_id"]),
                    text=transfer.message,
                )
                if sent:
                    with session_scope() as session:
                        stored = session.scalar(
                            select(ChainEvent)
                            .where(
                                ChainEvent.block_number == transfer.block_number,
                                ChainEvent.event_index == transfer.event_index,
                            )
                            .order_by(desc(ChainEvent.id))
                        )
                        if stored:
                            stored.notification_sent = True

    def _parse_transfer_attributes(self, attributes: Any) -> tuple[str | None, str | None, int | None]:
        if isinstance(attributes, dict):
            from_address = attributes.get("from") or attributes.get("who")
            to_address = attributes.get("to") or attributes.get("dest")
            amount = attributes.get("amount") or attributes.get("value")
            return str(from_address) if from_address else None, str(to_address) if to_address else None, self._to_int(amount)

        if not isinstance(attributes, list):
            return None, None, None

        flattened = [self._value_of(item) for item in attributes]
        if len(flattened) < 3:
            return None, None, None
        return (
            str(flattened[0]) if flattened[0] is not None else None,
            str(flattened[1]) if flattened[1] is not None else None,
            self._to_int(flattened[2]),
        )

    def _resolve_extrinsic_hash(self, extrinsics: list[Any], event_meta: dict[str, Any]) -> str | None:
        phase = event_meta.get("phase")
        if isinstance(phase, dict):
            extrinsic_idx = phase.get("ApplyExtrinsic")
        else:
            extrinsic_idx = None
        if extrinsic_idx is None:
            return None
        try:
            extrinsic = extrinsics[int(extrinsic_idx)]
        except (IndexError, TypeError, ValueError):
            return None

        if isinstance(extrinsic, dict):
            for key in ("extrinsic_hash", "hash"):
                if extrinsic.get(key):
                    return str(extrinsic[key])
        return getattr(extrinsic, "extrinsic_hash", None)

    def _pick_string(self, payload: dict[str, Any], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = payload.get(key)
            if value is not None:
                return str(value)
        return ""

    def _to_dict(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if hasattr(value, "to_dict"):
            converted = value.to_dict()
            if isinstance(converted, dict):
                return converted
        if hasattr(value, "__dict__"):
            return dict(value.__dict__)
        return {"value": str(value)}

    def _value_of(self, item: Any) -> Any:
        if isinstance(item, dict):
            for key in ("value", "account_id", "address"):
                if key in item:
                    return item[key]
            if len(item) == 1:
                return next(iter(item.values()))
        return item

    def _to_int(self, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            digits = value.replace(",", "")
            if digits.isdigit():
                return int(digits)
        return None


def ensure_state(session) -> MonitorState:
    state = session.get(MonitorState, 1)
    if state is None:
        state = MonitorState(id=1, monitor_status="idle", last_scanned_block=0, last_seen_head=0)
        session.add(state)
        session.flush()
    return state


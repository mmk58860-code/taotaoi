from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from substrateinterface import SubstrateInterface

from app.config import get_settings
from app.database import session_scope
from app.models import ChainEvent, MonitorMenu, MonitorState, NotificationOutbox, WalletWatch
from app.services.monitor_menu_service import BUILTIN_ALERT_KIND
from app.services.settings_service import get_system_runtime_settings, typed_system_runtime_settings
from app.services.taostats import TaoStatsClient


logger = logging.getLogger(__name__)
# TAO 和 Rao 的换算常量。
RAO_PER_TAO = 1_000_000_000
SS58_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{40,80}$")
HEX_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40,66}$")
IGNORED_ACTION_TYPES = {"evm_transact"}

WRAPPER_CALLS: dict[tuple[str, str], str] = {
    ("utility", "batch"): "批量调用",
    ("utility", "batch_all"): "批量调用",
    ("utility", "force_batch"): "强制批量调用",
    ("proxy", "proxy"): "代理调用",
    ("proxy", "proxy_announced"): "代理调用",
    ("multisig", "as_multi"): "多签调用",
    ("multisig", "as_multi_threshold_1"): "多签调用",
    ("multisig", "approve_as_multi"): "多签预批准",
    ("sudo", "sudo"): "Sudo 调用",
    ("sudo", "sudo_unchecked_weight"): "Sudo 调用",
    ("scheduler", "schedule"): "计划任务调用",
    ("scheduler", "schedule_named"): "计划任务调用",
}

ACTION_TITLES: dict[str, str] = {
    "transfer": "普通转账",
    "stake_add": "增加质押",
    "stake_remove": "减少质押",
    "stake_move": "移动质押",
    "stake_transfer": "转移质押",
    "stake_swap": "交换质押",
    "delegate_change": "委托相关操作",
    "root_register": "Root 注册",
    "burned_register": "燃烧注册",
    "subnet_register": "子网注册",
    "subnet_manage": "子网管理",
    "weights_set": "设置权重",
    "weights_commit": "提交权重承诺",
    "weights_reveal": "揭示权重",
    "axon_serve": "服务端点上报",
    "children_set": "子账户关系设置",
    "identity_set": "身份信息设置",
    "liquidity_manage": "流动性操作",
    "swap_call": "兑换操作",
    "registry_call": "注册表操作",
    "commitment_call": "承诺操作",
    "proxy_call": "代理调用",
    "multisig_call": "多签调用",
    "utility_batch": "批量调用",
    "evm_transact": "EVM 交易",
    "shielded_call": "MEV Shield 交易",
    "generic_call": "链上调用",
}


@dataclass
class NotificationProfile:
    monitor_menu_id: int
    owner_user_id: int
    menu_kind: str
    menu_name: str
    threshold_tao: float
    telegram_bot_token: str
    telegram_chat_id: str


@dataclass
class EventEnvelope:
    event_index: int
    extrinsic_index: int | None
    pallet: str
    event_name: str
    attributes: Any
    payload: dict[str, Any]


@dataclass
class CallEnvelope:
    pallet: str
    call_name: str
    params: dict[str, Any]
    wrapper_path: list[str]
    role_addresses: dict[str, str]
    raw_payload: dict[str, Any]


@dataclass
class ActionRecord:
    monitor_menu_id: int
    owner_user_id: int
    menu_name: str
    block_number: int
    event_index: int
    extrinsic_index: int
    pallet: str
    event_name: str
    action_type: str
    call_name: str
    amount_tao: float
    from_address: str | None
    to_address: str | None
    signer_address: str | None
    extrinsic_hash: str | None
    success: bool
    failure_reason: str | None
    involved_addresses: list[str]
    matched_aliases: list[str]
    message: str
    raw_payload: str
    should_notify: bool
    telegram_bot_token: str
    telegram_chat_id: str


@dataclass
class TaoPriceEstimate:
    amount_tao: float
    price_tao_per_alpha: float
    alpha_amount: float
    netuid: int
    source: str


@dataclass
class TaoStatsEstimate:
    amount_tao: float
    netuid: int | None
    source_payload: dict[str, Any]


class SubtensorMonitor:
    def __init__(self) -> None:
        # 监听任务会在 FastAPI 生命周期内启动和关闭。
        self._task: asyncio.Task[None] | None = None
        self._completion_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._wakeup_event = asyncio.Event()
        self._scan_lock = asyncio.Lock()
        self._last_reconciled_finalized_block = 0
        self._substrate: SubstrateInterface | None = None
        self._substrate_url = ""

    async def start(self) -> None:
        # 避免重复创建监听任务。
        self._stop_event.clear()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="subtensor-monitor")
        if self._completion_task is None or self._completion_task.done():
            self._completion_task = asyncio.create_task(self._run_amount_completion(), name="stake-amount-completion")

    async def stop(self) -> None:
        # 优雅停止后台扫描任务。
        self._stop_event.set()
        self._wakeup_event.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._completion_task:
            self._completion_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._completion_task
        self._close_substrate()

    async def restart(self) -> None:
        # 配置保存后唤醒当前循环，让新设置尽快生效。
        self._close_substrate()
        self._wakeup_event.set()

    async def _run(self) -> None:
        # 后台常驻循环：优先订阅新块头，订阅失败时再用短间隔轮询兜底。
        while not self._stop_event.is_set():
            try:
                if self._current_taostats_source_mode() == "only":
                    await self._run_taostats_source_loop()
                else:
                    await self._run_new_head_subscription()
            except Exception as exc:
                logger.exception("monitor loop failed")
                self._close_substrate()
                with session_scope() as session:
                    state = ensure_state(session)
                    state.monitor_status = "error"
                    state.last_error = str(exc)
                    state.updated_at = datetime.utcnow()
                await asyncio.sleep(10)
                continue

    def _current_taostats_source_mode(self) -> str:
        with session_scope() as session:
            raw = get_system_runtime_settings(session)
        typed = typed_system_runtime_settings(raw)
        return self._taostats_source_mode(str(typed["taostats_source_mode"]))

    async def _run_taostats_source_loop(self) -> None:
        # TaoStats 主数据源模式：不解码 Subtensor 区块，只按 TaoStats 返回的 undelegate 数据入库。
        while not self._stop_event.is_set() and not self._wakeup_event.is_set():
            completed = await asyncio.to_thread(self._scan_taostats_source_once)
            with session_scope() as session:
                raw = get_system_runtime_settings(session)
            typed = typed_system_runtime_settings(raw)
            wait_seconds = max(1, int(typed["taostats_poll_interval_seconds"]))
            if completed:
                wait_seconds = min(wait_seconds, 3)
            try:
                await asyncio.wait_for(self._wakeup_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
        self._wakeup_event.clear()

    def _scan_taostats_source_once(self) -> int:
        with session_scope() as session:
            raw_settings = get_system_runtime_settings(session)
            typed_settings = typed_system_runtime_settings(raw_settings)
            if not bool(typed_settings["taostats_enabled"]) or not (
                str(typed_settings["taostats_api_key"]) or str(typed_settings["taostats_api_keys"])
            ):
                return 0
            state = ensure_state(session)
            menu_rows = session.scalars(select(MonitorMenu).order_by(MonitorMenu.sort_order.asc(), MonitorMenu.id.asc())).all()
            wallet_rows = session.scalars(select(WalletWatch).where(WalletWatch.enabled.is_(True))).all()
            watch_map = self._build_watch_map(wallet_rows)
            profile_map = {
                row.id: NotificationProfile(
                    monitor_menu_id=row.id,
                    owner_user_id=row.owner_user_id,
                    menu_kind=row.menu_kind,
                    menu_name=row.name,
                    threshold_tao=float(row.large_transfer_threshold_tao),
                    telegram_bot_token=row.telegram_bot_token,
                    telegram_chat_id=row.telegram_chat_id,
                )
                for row in menu_rows
            }
            start_block = state.last_scanned_block + 1 if state.last_scanned_block > 0 else 0
            state.monitor_status = "running"
            state.last_error = None
            state.updated_at = datetime.utcnow()

        client = self._build_taostats_client(typed_settings)
        try:
            substrate = self._get_substrate(str(typed_settings["subtensor_ws_url"]))
            latest_block = self._get_latest_head_block(substrate)
        except Exception as exc:
            logger.info("免费链头读取失败，无法触发 TaoStats 主扫描 error=%s", exc)
            return 0

        lookback_start = max(1, latest_block - int(typed_settings["taostats_lookback_blocks"]))
        if start_block <= 0:
            start_block = lookback_start
        elif start_block < lookback_start:
            start_block = lookback_start
        logger.info(
            "TaoStats 主扫描区间 start=%s latest=%s lookback=%s",
            start_block,
            latest_block,
            int(typed_settings["taostats_lookback_blocks"]),
        )

        if start_block > latest_block:
            with session_scope() as session:
                state = ensure_state(session)
                state.last_seen_head = latest_block
                state.monitor_status = "running"
                state.updated_at = datetime.utcnow()
            return 0

        completed = 0
        for block_number in range(start_block, latest_block + 1):
            delegation_rows = client.fetch_stake_events(block_number=block_number, extrinsic_index=None, netuid=None, action="all")
            exchange_rows = client.fetch_exchange_events(block_number=block_number)
            actions = self._build_actions_from_taostats_rows(
                delegation_rows=delegation_rows,
                exchange_rows=exchange_rows,
                block_number=block_number,
                watch_map=watch_map,
                profile_map=profile_map,
            )
            if actions:
                asyncio.run(self._persist_and_notify(actions))
                completed += len(actions)
            with session_scope() as session:
                state = ensure_state(session)
                if actions or block_number < latest_block:
                    state.last_scanned_block = block_number
                state.last_seen_head = latest_block
                state.monitor_status = "running"
                state.updated_at = datetime.utcnow()
        return completed

    async def _run_amount_completion(self) -> None:
        # 减仓成交额补全器独立运行；查不到不影响主监听和 TG 推送。
        while not self._stop_event.is_set():
            try:
                completed_count = await asyncio.to_thread(self._complete_unresolved_stake_amounts_sync)
                wait_seconds = 8 if completed_count else 20
            except Exception:
                logger.exception("减仓成交额补全失败，稍后重试")
                wait_seconds = 20
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass

    def _complete_unresolved_stake_amounts_sync(self, limit: int = 6) -> int:
        with session_scope() as session:
            raw_settings = get_system_runtime_settings(session)
        typed_settings = typed_system_runtime_settings(raw_settings)
        if self._taostats_source_mode(str(typed_settings["taostats_source_mode"])) == "only":
            return 0

        taostats_mode = self._taostats_amount_mode(str(typed_settings["taostats_amount_mode"]))
        with session_scope() as session:
            raw_settings = get_system_runtime_settings(session)
            typed = typed_system_runtime_settings(raw_settings)
            cutoff = datetime.utcnow() - timedelta(seconds=15)
            conditions = [
                ChainEvent.action_type.in_(("stake_remove", "stake_swap", "swap_call")),
                ChainEvent.amount_tao <= 0,
                ChainEvent.detected_at <= cutoff,
                ~ChainEvent.raw_payload.contains('"tao_completion_status": "completed"'),
            ]
            if taostats_mode != "only":
                conditions.append(~ChainEvent.raw_payload.contains('"tao_estimate_status": "estimated"'))
            rows = session.scalars(
                select(ChainEvent)
                .where(*conditions)
                .order_by(ChainEvent.detected_at.desc())
                .limit(limit)
            ).all()

        if not rows:
            return 0

        substrate = SubstrateInterface(url=str(typed["subtensor_ws_url"]))
        taostats_client = self._build_taostats_client(typed_settings)
        completed_count = 0
        attempted_taostats = 0
        try:
            for row in rows:
                related_events: list[EventEnvelope] = []
                amount_tao = 0.0
                taostats_estimate = None
                price_estimate = None
                if taostats_mode in {"primary", "only"}:
                    if not self._taostats_retry_due(row, int(typed_settings["taostats_retry_cooldown_seconds"])):
                        continue
                    if attempted_taostats >= 3:
                        logger.info("本轮 TaoStats 免费额度保护：已尝试 3 条，暂停到下一轮")
                        break
                    taostats_estimate = self._estimate_taostats_amount_tao(row, taostats_client)
                    attempted_taostats += 1

                if taostats_estimate is None and taostats_mode != "only":
                    related_events = self._fetch_related_events_for_chain_event(substrate, row)
                    amount_tao = self._estimate_amount_tao_from_events(row.action_type, related_events)

                if (
                    amount_tao <= 0
                    and taostats_estimate is None
                    and taostats_mode == "fallback"
                    and self._taostats_retry_due(row, int(typed_settings["taostats_retry_cooldown_seconds"]))
                ):
                    if attempted_taostats >= 3:
                        logger.info("本轮 TaoStats 免费额度保护：已尝试 3 条，暂停到下一轮")
                        break
                    taostats_estimate = self._estimate_taostats_amount_tao(row, taostats_client)
                    attempted_taostats += 1

                if amount_tao <= 0 and taostats_estimate is None and taostats_mode != "only":
                    price_estimate = self._estimate_subnet_price_tao(substrate, row)

                self._store_completion_result(
                    row.id,
                    related_events,
                    amount_tao,
                    taostats_estimate,
                    price_estimate,
                    taostats_only=taostats_mode == "only",
                )
                if amount_tao > 0 or taostats_estimate is not None or price_estimate is not None:
                    completed_count += 1
        finally:
            close = getattr(substrate, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
        return completed_count

    def _fetch_related_events_for_chain_event(
        self,
        substrate: SubstrateInterface,
        row: ChainEvent,
    ) -> list[EventEnvelope]:
        block_hash = substrate.get_block_hash(row.block_number)
        events = substrate.get_events(block_hash=block_hash)
        event_rows = [self._normalize_event(event, idx) for idx, event in enumerate(events)]
        grouped = self._group_events_by_extrinsic(event_rows)
        related = grouped.get(int(row.extrinsic_index or 0), [])
        if related:
            return related
        # 某些节点返回的 phase 结构解析不到时，保守返回空，避免误吃同块其他 extrinsic 的余额事件。
        return []

    def _estimate_amount_tao_from_events(self, action_type: str, related_events: list[EventEnvelope]) -> float:
        candidates: list[int] = []
        candidates.extend(self._collect_settlement_tao_from_events(action_type, related_events))
        candidates.extend(self._collect_balance_tao_from_events(action_type, related_events))
        if not candidates:
            return 0.0
        return round(max(candidates) / RAO_PER_TAO, 9)

    def _build_taostats_client(self, settings) -> TaoStatsClient:
        if isinstance(settings, dict):
            api_key = str(settings.get("taostats_api_key", ""))
            api_keys = str(settings.get("taostats_api_keys", ""))
            request_interval_seconds = float(settings.get("taostats_request_interval_seconds", 1))
            rate_limit_cooldown_seconds = int(settings.get("taostats_rate_limit_cooldown_seconds", 15))
        else:
            api_key = settings.taostats_api_key
            api_keys = settings.taostats_api_keys
            request_interval_seconds = settings.taostats_request_interval_seconds
            rate_limit_cooldown_seconds = settings.taostats_rate_limit_cooldown_seconds
        return TaoStatsClient(
            api_key,
            api_keys=api_keys,
            request_interval_seconds=request_interval_seconds,
            rate_limit_cooldown_seconds=rate_limit_cooldown_seconds,
        )

    def _build_actions_from_taostats_rows(
        self,
        delegation_rows: list[dict[str, Any]],
        exchange_rows: list[dict[str, Any]],
        block_number: int,
        watch_map: dict[str, dict[int, list[str]]],
        profile_map: dict[int, NotificationProfile],
    ) -> list[ActionRecord]:
        actions: list[ActionRecord] = []
        alert_profiles = {
            menu_id: profile
            for menu_id, profile in profile_map.items()
            if profile.menu_kind == BUILTIN_ALERT_KIND and profile.threshold_tao > 0
        }
        delegation_count = len(delegation_rows)
        exchange_count = len(exchange_rows)
        delegation_supported = 0
        delegation_positive = 0
        delegation_matched = 0
        exchange_supported = 0
        exchange_positive = 0
        exchange_matched = 0
        for row_index, row in enumerate(delegation_rows):
            action_type = self._taostats_row_action_type(row)
            if action_type not in {"stake_add", "stake_remove"}:
                continue
            delegation_supported += 1
            amount_tao = self._extract_taostats_tao_amount(row)
            if amount_tao <= 0:
                continue
            delegation_positive += 1
            involved_addresses = self._collect_addresses(row)
            extrinsic_index = self._taostats_extrinsic_index(row, row_index)
            event_index = extrinsic_index * 1000 + row_index
            matched_menu_ids: set[int] = set()
            for address in involved_addresses:
                matched_menu_ids.update(watch_map.get(address, {}).keys())
            for menu_id, profile in alert_profiles.items():
                if amount_tao >= profile.threshold_tao:
                    matched_menu_ids.add(menu_id)
            if not matched_menu_ids:
                continue
            delegation_matched += 1

            primary_from, primary_to = self._taostats_primary_route(row, involved_addresses)
            signer_address = self._pick_first_address(
                row.get("coldkey"),
                row.get("coldkey_ss58"),
                row.get("account"),
                row.get("address"),
                row.get("delegator"),
                row.get("owner"),
                primary_from,
            )
            netuid = self._taostats_netuid(row)
            raw_payload = {
                "source": "taostats",
                "tao_completion_status": "completed",
                "tao_completion_source": "taostats",
                "action_type": action_type,
                "taostats_result": row,
                "involved_addresses": involved_addresses,
                "taostats_netuid": netuid,
            }
            for monitor_menu_id in matched_menu_ids:
                profile = profile_map.get(monitor_menu_id)
                if profile is None:
                    continue
                matched_aliases = self._collect_aliases(
                    watch_map=watch_map,
                    monitor_menu_id=monitor_menu_id,
                    involved_addresses=involved_addresses,
                )
                watched = bool(matched_aliases)
                above_threshold = profile.menu_kind == BUILTIN_ALERT_KIND and amount_tao >= profile.threshold_tao
                message = self._build_taostats_message(
                    action_type=action_type,
                    amount_tao=amount_tao,
                    block_number=block_number,
                    extrinsic_index=extrinsic_index,
                    netuid=netuid,
                    signer_address=signer_address,
                    primary_from=primary_from,
                    primary_to=primary_to,
                    involved_addresses=involved_addresses,
                    matched_aliases=matched_aliases,
                    watched=watched,
                    above_threshold=above_threshold,
                    threshold_tao=profile.threshold_tao,
                )
                should_notify = self._should_notify_action(
                    profile=profile,
                    action_type=action_type,
                    watched=watched,
                    above_threshold=above_threshold,
                )
                actions.append(
                    ActionRecord(
                        monitor_menu_id=monitor_menu_id,
                        owner_user_id=profile.owner_user_id,
                        menu_name=profile.menu_name,
                        block_number=block_number,
                        event_index=event_index,
                        extrinsic_index=extrinsic_index,
                        pallet="TaoStats",
                        event_name=str(row.get("action") or "taostats_event").lower(),
                        action_type=action_type,
                        call_name=f"taostats_{str(row.get('action') or 'event').lower()}",
                        amount_tao=amount_tao,
                        from_address=primary_from,
                        to_address=primary_to,
                        signer_address=signer_address,
                        extrinsic_hash=str(row.get("extrinsic_hash") or row.get("hash") or "") or None,
                        success=True,
                        failure_reason=None,
                        involved_addresses=involved_addresses,
                        matched_aliases=matched_aliases,
                        message=message,
                        raw_payload=json.dumps(raw_payload, ensure_ascii=False, default=str),
                        should_notify=should_notify,
                        telegram_bot_token=profile.telegram_bot_token,
                        telegram_chat_id=profile.telegram_chat_id,
                    )
                )
        for row_index, row in enumerate(exchange_rows, start=1000):
            action_type = self._taostats_exchange_action_type(row)
            if action_type not in {"stake_swap", "swap_call"}:
                continue
            exchange_supported += 1
            amount_tao = self._extract_taostats_exchange_tao_amount(row)
            if amount_tao <= 0:
                continue
            exchange_positive += 1
            involved_addresses = self._collect_addresses(row)
            extrinsic_index = self._taostats_extrinsic_index(row, row_index)
            event_index = extrinsic_index * 1000 + row_index
            matched_menu_ids: set[int] = set()
            for address in involved_addresses:
                matched_menu_ids.update(watch_map.get(address, {}).keys())
            for menu_id, profile in alert_profiles.items():
                if amount_tao >= profile.threshold_tao:
                    matched_menu_ids.add(menu_id)
            if not matched_menu_ids:
                continue
            exchange_matched += 1

            primary_from, primary_to = self._taostats_primary_route(row, involved_addresses)
            signer_address = self._pick_first_address(
                row.get("account"),
                row.get("owner"),
                row.get("address"),
                primary_from,
            )
            netuid = self._taostats_netuid(row)
            raw_payload = {
                "source": "taostats",
                "tao_completion_status": "completed",
                "tao_completion_source": "taostats",
                "action_type": action_type,
                "taostats_result": row,
                "involved_addresses": involved_addresses,
                "taostats_netuid": netuid,
            }
            for monitor_menu_id in matched_menu_ids:
                profile = profile_map.get(monitor_menu_id)
                if profile is None:
                    continue
                matched_aliases = self._collect_aliases(
                    watch_map=watch_map,
                    monitor_menu_id=monitor_menu_id,
                    involved_addresses=involved_addresses,
                )
                watched = bool(matched_aliases)
                above_threshold = profile.menu_kind == BUILTIN_ALERT_KIND and amount_tao >= profile.threshold_tao
                message = self._build_taostats_message(
                    action_type=action_type,
                    amount_tao=amount_tao,
                    block_number=block_number,
                    extrinsic_index=extrinsic_index,
                    netuid=netuid,
                    signer_address=signer_address,
                    primary_from=primary_from,
                    primary_to=primary_to,
                    involved_addresses=involved_addresses,
                    matched_aliases=matched_aliases,
                    watched=watched,
                    above_threshold=above_threshold,
                    threshold_tao=profile.threshold_tao,
                )
                should_notify = self._should_notify_action(
                    profile=profile,
                    action_type=action_type,
                    watched=watched,
                    above_threshold=above_threshold,
                )
                actions.append(
                    ActionRecord(
                        monitor_menu_id=monitor_menu_id,
                        owner_user_id=profile.owner_user_id,
                        menu_name=profile.menu_name,
                        block_number=block_number,
                        event_index=event_index,
                        extrinsic_index=extrinsic_index,
                        pallet="TaoStats",
                        event_name=str(row.get("action") or row.get("type") or "exchange").lower(),
                        action_type=action_type,
                        call_name=f"taostats_{str(row.get('action') or row.get('type') or 'exchange').lower()}",
                        amount_tao=amount_tao,
                        from_address=primary_from,
                        to_address=primary_to,
                        signer_address=signer_address,
                        extrinsic_hash=str(row.get("extrinsic_hash") or row.get("hash") or "") or None,
                        success=True,
                        failure_reason=None,
                        involved_addresses=involved_addresses,
                        matched_aliases=matched_aliases,
                        message=message,
                        raw_payload=json.dumps(raw_payload, ensure_ascii=False, default=str),
                        should_notify=should_notify,
                        telegram_bot_token=profile.telegram_bot_token,
                        telegram_chat_id=profile.telegram_chat_id,
                    )
                )

        if delegation_count or exchange_count:
            logger.info(
                "TAOSTATS_PARSE block=%s delegation_rows=%s exchange_rows=%s actions=%s alert_profiles=%s wallets=%s",
                block_number,
                delegation_count,
                exchange_count,
                len(actions),
                len(alert_profiles),
                len(watch_map),
            )
            logger.info(
                "TAOSTATS_FILTERS block=%s delegation_supported=%s delegation_positive=%s delegation_matched=%s exchange_supported=%s exchange_positive=%s exchange_matched=%s",
                block_number,
                delegation_supported,
                delegation_positive,
                delegation_matched,
                exchange_supported,
                exchange_positive,
                exchange_matched,
            )
        return actions

    def _build_taostats_message(
        self,
        action_type: str,
        amount_tao: float,
        block_number: int,
        extrinsic_index: int,
        netuid: int | None,
        signer_address: str | None,
        primary_from: str | None,
        primary_to: str | None,
        involved_addresses: list[str],
        matched_aliases: list[str],
        watched: bool,
        above_threshold: bool,
        threshold_tao: float,
    ) -> str:
        tags: list[str] = []
        if watched:
            tags.append(f"监控钱包: {', '.join(matched_aliases)}")
        if above_threshold:
            tags.append(f"大额阈值: >= {threshold_tao} TAO")
        title = "🟢 增加质押" if action_type == "stake_add" else "🔴 减少质押"
        direction = "买入 / 加仓" if action_type == "stake_add" else "卖出 / 减仓"
        signal = "TaoStats 加仓" if action_type == "stake_add" else "TaoStats 减仓"
        lines = [
            f"<b>{title}</b>",
            "状态: <b>成功</b>",
            f"调用: <code>TaoStats.{ 'delegate' if action_type == 'stake_add' else 'undelegate' }</code>",
            f"子网: <b>{f'子网 {netuid}' if netuid is not None else '未知子网'}</b>",
            f"方向: <b>{direction}</b>",
            f"信号: <b>{signal}</b>",
            f"区块: <code>{block_number}</code>",
            f"Extrinsic: <code>{extrinsic_index}</code>",
            f"签名者: <code>{signer_address or '-'}</code>",
            f"金额估值: <b>{amount_tao:.6f} TAO（TaoStats）</b>",
            f"主路径: <code>{primary_from or '-'} -> {primary_to or '-'}</code>",
            f"关联地址: <code>{', '.join(involved_addresses[:8]) if involved_addresses else '-'}</code>",
        ]
        if tags:
            lines.append(f"命中原因: {', '.join(tags)}")
        return "\n".join(lines)

    def _estimate_taostats_amount_tao(
        self,
        row: ChainEvent,
        client: TaoStatsClient | None = None,
    ) -> TaoStatsEstimate | None:
        with session_scope() as session:
            raw = get_system_runtime_settings(session)
        typed = typed_system_runtime_settings(raw)
        if not bool(typed["taostats_enabled"]) or not (str(typed["taostats_api_key"]) or str(typed["taostats_api_keys"])):
            return None
        if row.action_type != "stake_remove":
            return None
        payload = self._safe_json_loads(row.raw_payload)
        if not isinstance(payload, dict):
            return None
        params = payload.get("leaf_call", payload)
        subnet_ids = self._extract_subnet_ids(params)
        netuid = subnet_ids[0] if subnet_ids else None

        if client is None:
            client = self._build_taostats_client(typed)
        rows = client.fetch_stake_events(
            block_number=int(row.block_number),
            extrinsic_index=int(row.extrinsic_index or 0),
            netuid=None,
        )
        for item in rows:
            amount_tao = self._extract_taostats_tao_amount(item)
            if amount_tao > 0:
                return TaoStatsEstimate(
                    amount_tao=amount_tao,
                    netuid=netuid,
                    source_payload=item,
                )
        return None

    def _taostats_retry_due(self, row: ChainEvent, cooldown_seconds: int) -> bool:
        payload = self._safe_json_loads(row.raw_payload)
        if not isinstance(payload, dict):
            return True
        checked_at = payload.get("taostats_checked_at") or payload.get("tao_completion_checked_at")
        if not checked_at:
            return True
        try:
            checked_dt = datetime.fromisoformat(str(checked_at))
        except ValueError:
            return True
        return datetime.utcnow() - checked_dt >= timedelta(seconds=max(10, int(cooldown_seconds)))

    def _taostats_amount_mode(self, value: str) -> str:
        mode = str(value or "fallback").strip().lower()
        if mode in {"fallback", "primary", "only"}:
            return mode
        return "fallback"

    def _taostats_source_mode(self, value: str) -> str:
        mode = str(value or "chain").strip().lower()
        if mode in {"chain", "only"}:
            return mode
        return "chain"

    def _taostats_extrinsic_index(self, row: dict[str, Any], fallback: int) -> int:
        for key in ("extrinsic_index", "extrinsic_idx", "extrinsicIndex"):
            parsed = self._to_int(row.get(key))
            if parsed is not None:
                return parsed
        extrinsic_id = str(row.get("extrinsic_id") or row.get("extrinsicId") or "")
        match = re.search(r"[-:](\d+)(?:[-:]|$)", extrinsic_id)
        if match:
            return int(match.group(1))
        return int(fallback)

    def _taostats_netuid(self, row: dict[str, Any]) -> int | None:
        for key in ("netuid", "net_uid", "subnet", "subnet_id"):
            parsed = self._to_int(row.get(key))
            if parsed is not None and 0 <= parsed <= 10_000:
                return parsed
        subnet_ids = self._extract_subnet_ids(row)
        return subnet_ids[0] if subnet_ids else None

    def _taostats_primary_route(self, row: dict[str, Any], involved_addresses: list[str]) -> tuple[str | None, str | None]:
        from_address = self._pick_first_address(
            row.get("coldkey"),
            row.get("coldkey_ss58"),
            row.get("nominator"),
            row.get("delegator"),
            row.get("account"),
            row.get("owner"),
        )
        to_address = self._pick_first_address(
            row.get("hotkey"),
            row.get("hotkey_ss58"),
            row.get("delegate"),
            row.get("validator"),
            row.get("delegate_name"),
        )
        if from_address or to_address:
            return from_address, to_address
        if len(involved_addresses) >= 2:
            return involved_addresses[0], involved_addresses[1]
        if len(involved_addresses) == 1:
            return involved_addresses[0], None
        return None, None

    def _taostats_row_action_type(self, row: dict[str, Any]) -> str:
        action = str(row.get("action") or "").strip().upper()
        if action == "DELEGATE":
            return "stake_add"
        if action == "UNDELEGATE":
            return "stake_remove"
        return "generic_call"

    def _taostats_exchange_action_type(self, row: dict[str, Any]) -> str:
        action = str(row.get("action") or row.get("type") or "").strip().upper()
        if "SWAP" in action or "EXCHANGE" in action:
            return "stake_swap"
        return "generic_call"

    def _extract_taostats_tao_amount(self, payload: Any) -> float:
        # TaoStats 返回字段可能随接口版本变化，优先读取明确带 TAO/rao 语义且不含 alpha 的金额字段。
        candidates = self._collect_tao_amount_candidates(payload)
        if candidates:
            return round(max(candidates) / RAO_PER_TAO, 9)
        normalized = self._normalize_value(payload)
        if not isinstance(normalized, dict):
            return 0.0
        # TaoStats delegation/v1 的 amount 字段就是实际成交的 TAO，单位为 Rao。
        amount_raw = self._to_int(normalized.get("amount"))
        if amount_raw is not None and amount_raw > 0:
            return round(amount_raw / RAO_PER_TAO, 9)
        for key in ("amount_tao", "tao_amount", "tao", "received_tao", "tao_received"):
            value = normalized.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return round(float(value), 9)
            if isinstance(value, str):
                with suppress(ValueError):
                    parsed = float(value.replace(",", "").replace("_", ""))
                    if parsed > 0:
                        return round(parsed, 9)
        return 0.0

    def _extract_taostats_exchange_tao_amount(self, payload: Any) -> float:
        normalized = self._normalize_value(payload)
        if not isinstance(normalized, dict):
            return 0.0
        for key in (
            "tao_amount",
            "amount_tao",
            "received_tao",
            "tao_received",
            "source_tao",
            "destination_tao",
            "input_tao",
            "output_tao",
        ):
            value = normalized.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return round(float(value), 9)
            if isinstance(value, str):
                with suppress(ValueError):
                    parsed = float(value.replace(",", "").replace("_", ""))
                    if parsed > 0:
                        return round(parsed, 9)
        amount_raw = self._to_int(normalized.get("amount"))
        if amount_raw is not None and amount_raw > 0:
            return round(amount_raw / RAO_PER_TAO, 9)
        return 0.0

    def _estimate_subnet_price_tao(
        self,
        substrate: SubstrateInterface,
        row: ChainEvent,
    ) -> TaoPriceEstimate | None:
        # 只有减仓才是 Alpha 卖回 TAO；换仓是 Alpha 到 Alpha，不能直接当 TAO 估值。
        if row.action_type != "stake_remove":
            return None
        payload = self._safe_json_loads(row.raw_payload)
        if not isinstance(payload, dict):
            return None
        params = payload.get("leaf_call", payload)
        block_hash = substrate.get_block_hash(row.block_number)
        return self._estimate_subnet_price_from_params(
            substrate=substrate,
            action_type=row.action_type,
            params=params,
            block_number=row.block_number,
            block_hash=block_hash,
        )

    def _estimate_subnet_price_from_params(
        self,
        substrate: SubstrateInterface,
        action_type: str,
        params: Any,
        block_number: int,
        block_hash: str | None,
    ) -> TaoPriceEstimate | None:
        # 实时扫块和后台补全共用这段逻辑：只把 Alpha 卖出按子网价格折算成 TAO 估算。
        if action_type != "stake_remove":
            return None
        alpha_candidates = self._collect_alpha_amount_candidates(params)
        subnet_ids = self._extract_subnet_ids(params)
        if not alpha_candidates or not subnet_ids:
            return None

        alpha_amount = max(alpha_candidates) / RAO_PER_TAO
        for netuid in subnet_ids:
            price = self._query_subnet_price_tao_per_alpha(substrate, netuid, block_hash)
            if price <= 0:
                continue
            amount_tao = round(alpha_amount * price, 9)
            if amount_tao > 0:
                return TaoPriceEstimate(
                    amount_tao=amount_tao,
                    price_tao_per_alpha=round(price, 12),
                    alpha_amount=round(alpha_amount, 9),
                    netuid=netuid,
                    source="subnet_price",
                )
        logger.info(
            "未拿到子网价格估算 block=%s netuids=%s alpha=%s",
            block_number,
            subnet_ids,
            round(alpha_amount, 9),
        )
        return None

    def _query_subnet_price_tao_per_alpha(
        self,
        substrate: SubstrateInterface,
        netuid: int,
        block_hash: str | None,
    ) -> float:
        if netuid == 0:
            return 1.0
        storage_candidates = (
            ("Swap", "AlphaSqrtPrice"),
            ("SubtensorModule", "AlphaSqrtPrice"),
        )
        for module, storage_function in storage_candidates:
            for candidate_hash in (block_hash, None):
                try:
                    raw_price = substrate.query(
                        module=module,
                        storage_function=storage_function,
                        params=[netuid],
                        block_hash=candidate_hash,
                    )
                except Exception as exc:
                    logger.debug(
                        "查询子网价格失败 module=%s storage=%s netuid=%s block_hash=%s error=%s",
                        module,
                        storage_function,
                        netuid,
                        bool(candidate_hash),
                        exc,
                    )
                    continue
                sqrt_price = self._fixed_to_float(raw_price)
                price = sqrt_price * sqrt_price
                if math.isfinite(price) and price > 0:
                    return price
        return 0.0

    def _fixed_to_float(self, value: Any, *, frac_bits: int = 64, total_bits: int = 128) -> float:
        # Bittensor SDK 的子网价格来自定点 sqrt_price；这里复刻轻量换算，避免引入完整 SDK。
        normalized = self._normalize_value(value)
        if isinstance(normalized, float) and math.isfinite(normalized):
            return normalized
        raw = self._to_int(value)
        if raw is None:
            return 0.0
        sign_bit = 1 << (total_bits - 1)
        if raw & sign_bit:
            raw -= 1 << total_bits
        return raw / float(1 << frac_bits)

    def _store_completion_result(
        self,
        event_id: int,
        related_events: list[EventEnvelope],
        amount_tao: float,
        taostats_estimate: TaoStatsEstimate | None = None,
        price_estimate: TaoPriceEstimate | None = None,
        taostats_only: bool = False,
    ) -> None:
        with session_scope() as session:
            row = session.get(ChainEvent, event_id)
            if row is None or row.amount_tao > 0:
                return
            payload = self._safe_json_loads(row.raw_payload)
            if not isinstance(payload, dict):
                payload = {}
            if related_events:
                payload["related_events"] = [event.payload for event in related_events]
            checked_at = datetime.utcnow().isoformat()
            payload["tao_completion_checked_at"] = checked_at
            if taostats_estimate is not None or taostats_only:
                payload["taostats_checked_at"] = checked_at
            if amount_tao > 0:
                row.amount_tao = amount_tao
                payload["tao_completion_status"] = "completed"
                row.message = self._replace_unconfirmed_amount_label(row.message, amount_tao)
                logger.info("已补全减仓成交额 event_id=%s amount_tao=%s", event_id, amount_tao)
            elif taostats_estimate is not None:
                row.amount_tao = taostats_estimate.amount_tao
                payload["tao_completion_status"] = "completed"
                payload["tao_completion_source"] = "taostats"
                payload["taostats_result"] = taostats_estimate.source_payload
                payload["taostats_netuid"] = taostats_estimate.netuid
                row.message = self._replace_amount_label_with_text(
                    row.message,
                    f"{taostats_estimate.amount_tao:.6f} TAO（TaoStats）",
                )
                logger.info("已用 TaoStats 补全减仓成交额 event_id=%s amount_tao=%s", event_id, taostats_estimate.amount_tao)
            elif price_estimate is not None:
                payload["tao_completion_status"] = "not_found"
                payload["tao_estimate_status"] = "estimated"
                payload["tao_estimate"] = {
                    "source": price_estimate.source,
                    "amount_tao": price_estimate.amount_tao,
                    "price_tao_per_alpha": price_estimate.price_tao_per_alpha,
                    "alpha_amount": price_estimate.alpha_amount,
                    "netuid": price_estimate.netuid,
                    "block_number": row.block_number,
                    "calculated_at": datetime.utcnow().isoformat(),
                }
                row.message = self._replace_amount_label_with_text(
                    row.message,
                    f"约 {price_estimate.amount_tao:.6f} TAO（按子网价格估算）",
                )
                logger.info(
                    "已按子网价格估算减仓 TAO event_id=%s netuid=%s amount_tao=%s",
                    event_id,
                    price_estimate.netuid,
                    price_estimate.amount_tao,
                )
            else:
                payload["tao_completion_status"] = "not_found"
                if taostats_only:
                    payload["taostats_status"] = "not_found"
                payload["tao_estimate_status"] = "not_available"
            row.raw_payload = json.dumps(payload, ensure_ascii=False, default=str)

    def _replace_unconfirmed_amount_label(self, message: str, amount_tao: float) -> str:
        return self._replace_amount_label_with_text(message, f"{amount_tao:.6f} TAO（补全）")

    def _replace_amount_label_with_text(self, message: str, amount_label: str) -> str:
        amount_label = f"金额估值: <b>{amount_label}</b>"
        if "金额估值: <b>" not in message:
            return message
        return re.sub(r"金额估值: <b>.*?</b>", amount_label, message, count=1)

    def _current_poll_interval(self) -> int:
        # 链路级扫描间隔属于系统配置，只需读取一次总管理员维护的设置。
        with session_scope() as session:
            raw = get_system_runtime_settings(session)
        settings = typed_system_runtime_settings(raw)
        return int(settings["poll_interval_seconds"])

    async def _run_new_head_subscription(self) -> None:
        # 启动时先追到当前最新块，然后等待链节点推送新块头。
        with session_scope() as session:
            raw_settings = get_system_runtime_settings(session)
        typed = typed_system_runtime_settings(raw_settings)
        url = str(typed["subtensor_ws_url"])

        await self._scan_once()
        loop = asyncio.get_running_loop()

        def subscription_worker() -> None:
            subscription_substrate = SubstrateInterface(url=url)

            def on_new_head(obj: Any, update_nr: int, subscription_id: str) -> dict[str, Any] | None:
                if self._stop_event.is_set():
                    return {"status": "监听已停止"}
                if self._wakeup_event.is_set():
                    return {"status": "配置已重新加载"}

                block_number = self._header_block_number(obj)
                if block_number <= 0:
                    return None

                future = asyncio.run_coroutine_threadsafe(self._scan_once(triggered_head_block=block_number), loop)
                future.result()
                return None

            try:
                subscription_substrate.subscribe_block_headers(on_new_head)
            finally:
                close = getattr(subscription_substrate, "close", None)
                if callable(close):
                    with suppress(Exception):
                        close()

        try:
            await asyncio.to_thread(subscription_worker)
        except Exception:
            logger.exception("新块头订阅失败，改用备用轮询")
            await self._run_polling_fallback()
        finally:
            self._wakeup_event.clear()

    async def _run_polling_fallback(self) -> None:
        # 免费节点偶尔会断开订阅；这里按系统设置的备用间隔轮询顶上，下一轮再尝试恢复订阅。
        while not self._stop_event.is_set() and not self._wakeup_event.is_set():
            await self._scan_once()
            wait_seconds = self._current_poll_interval()
            try:
                await asyncio.wait_for(self._wakeup_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                return

    async def _scan_once(self, triggered_head_block: int | None = None) -> None:
        # 先处理最新可见区块争取首见，再用最终确认区块补漏校对。
        async with self._scan_lock:
            await self._scan_once_locked(triggered_head_block=triggered_head_block)

    async def _scan_once_locked(self, triggered_head_block: int | None = None) -> None:
        with session_scope() as session:
            raw_settings = get_system_runtime_settings(session)
            typed = typed_system_runtime_settings(raw_settings)
            state = ensure_state(session)
            menu_rows = session.scalars(select(MonitorMenu).order_by(MonitorMenu.sort_order.asc(), MonitorMenu.id.asc())).all()
            wallet_rows = session.scalars(select(WalletWatch).where(WalletWatch.enabled.is_(True))).all()
            watch_map = self._build_watch_map(wallet_rows)
            profile_map = {
                row.id: NotificationProfile(
                    monitor_menu_id=row.id,
                    owner_user_id=row.owner_user_id,
                    menu_kind=row.menu_kind,
                    menu_name=row.name,
                    threshold_tao=float(row.large_transfer_threshold_tao),
                    telegram_bot_token=row.telegram_bot_token,
                    telegram_chat_id=row.telegram_chat_id,
                )
                for row in menu_rows
            }
            state.monitor_status = "running"
            state.last_error = None

        substrate = self._get_substrate(str(typed["subtensor_ws_url"]))
        if triggered_head_block is None:
            latest_head_block = await asyncio.to_thread(self._get_latest_head_block, substrate)
        else:
            latest_head_block = int(triggered_head_block)
        finalized_block = await asyncio.to_thread(self._get_latest_finalized_block, substrate)
        finalized_target = max(0, int(finalized_block) - int(typed["finality_lag_blocks"]))

        with session_scope() as session:
            state = ensure_state(session)
            start_block = state.last_scanned_block + 1 if state.last_scanned_block else max(latest_head_block - 20, 1)
            state.last_seen_head = int(latest_head_block)

        await self._scan_block_range(
            substrate=substrate,
            start_block=start_block,
            target_block=latest_head_block,
            watch_map=watch_map,
            profile_map=profile_map,
            update_progress=True,
        )

        if finalized_target > self._last_reconciled_finalized_block:
            reconcile_start = (
                max(1, finalized_target - 20)
                if self._last_reconciled_finalized_block <= 0
                else max(1, self._last_reconciled_finalized_block)
            )
            await self._scan_block_range(
                substrate=substrate,
                start_block=reconcile_start,
                target_block=finalized_target,
                watch_map=watch_map,
                profile_map=profile_map,
                update_progress=False,
            )
            self._last_reconciled_finalized_block = finalized_target

    async def _scan_block_range(
        self,
        substrate: SubstrateInterface,
        start_block: int,
        target_block: int,
        watch_map: dict[str, dict[int, list[str]]],
        profile_map: dict[int, NotificationProfile],
        update_progress: bool,
    ) -> None:
        if start_block > target_block:
            return

        for block_number in range(start_block, target_block + 1):
            try:
                actions = await asyncio.to_thread(
                    self._extract_actions_sync,
                    substrate,
                    block_number,
                    watch_map,
                    profile_map,
                )
            except Exception:
                logger.exception("区块 %s 扫描失败，下一轮会重试", block_number)
                break
            await self._persist_and_notify(actions)
            if update_progress:
                with session_scope() as session:
                    state = ensure_state(session)
                    state.last_scanned_block = block_number
                    state.updated_at = datetime.utcnow()

    def _build_watch_map(self, wallet_rows: list[WalletWatch]) -> dict[str, dict[int, list[str]]]:
        # 同一个地址允许被多个监控菜单分别监控，所以用 address -> menu_id -> aliases 的结构。
        watch_map: dict[str, dict[int, list[str]]] = {}
        for row in wallet_rows:
            watch_map.setdefault(row.address, {}).setdefault(row.monitor_menu_id, []).append(row.alias)
        return watch_map

    def _get_substrate(self, url: str) -> SubstrateInterface:
        # 快速监听模式复用同一条链节点连接，避免每轮扫描都重新握手。
        if self._substrate is None or self._substrate_url != url:
            self._close_substrate()
            self._substrate = SubstrateInterface(url=url)
            self._substrate_url = url
        return self._substrate

    def _close_substrate(self) -> None:
        # 不同版本的底层连接关闭方法不完全一致，所以这里做兼容处理。
        substrate = self._substrate
        self._substrate = None
        self._substrate_url = ""
        close = getattr(substrate, "close", None)
        if callable(close):
            with suppress(Exception):
                close()

    def _extract_actions_sync(
        self,
        substrate: SubstrateInterface,
        block_number: int,
        watch_map: dict[str, dict[int, list[str]]],
        profile_map: dict[int, NotificationProfile],
    ) -> list[ActionRecord]:
        # 每个区块都读取全部 extrinsic 和 event，再展开成统一动作。
        with session_scope() as session:
            raw_runtime = get_system_runtime_settings(session)
        typed_runtime = typed_system_runtime_settings(raw_runtime)
        block_hash = substrate.get_block_hash(block_number)
        block = substrate.get_block(block_hash=block_hash)
        events = substrate.get_events(block_hash=block_hash)

        extrinsic_rows = self._extract_extrinsic_rows(block)
        event_rows = [self._normalize_event(event, idx) for idx, event in enumerate(events)]
        events_by_extrinsic = self._group_events_by_extrinsic(event_rows)

        threshold_menu_ids = {
            monitor_menu_id
            for monitor_menu_id, profile in profile_map.items()
            if profile.menu_kind == BUILTIN_ALERT_KIND and profile.threshold_tao > 0
        }

        results: list[ActionRecord] = []
        for extrinsic_index, extrinsic in enumerate(extrinsic_rows):
            extrinsic_payload = self._normalize_extrinsic(extrinsic, extrinsic_index)
            related_events = events_by_extrinsic.get(extrinsic_index, [])
            success, failure_reason = self._resolve_success(related_events)
            leaf_calls = self._flatten_calls(
                extrinsic_payload["call"],
                signer_address=extrinsic_payload["signer_address"],
                wrapper_path=[],
                inherited_roles={"signer": extrinsic_payload["signer_address"]} if extrinsic_payload["signer_address"] else {},
            )
            if not leaf_calls:
                continue

            for leaf_call_index, leaf_call in enumerate(leaf_calls):
                involved_addresses = self._build_involved_addresses(
                    leaf_call,
                    extrinsic_payload["signer_address"],
                    related_events,
                )
                amount_tao = self._estimate_amount_tao(leaf_call, related_events)
                action_type = self._classify_action_type(leaf_call.pallet, leaf_call.call_name)
                if self._should_ignore_action(action_type):
                    continue
                if not success and self._is_trade_action(action_type):
                    continue
                taostats_mode = self._taostats_amount_mode(str(typed_runtime["taostats_amount_mode"]))
                if taostats_mode == "only" and action_type == "stake_remove":
                    amount_tao = 0.0
                price_estimate = None
                if amount_tao <= 0 and taostats_mode != "only":
                    price_estimate = self._estimate_subnet_price_from_params(
                        substrate=substrate,
                        action_type=action_type,
                        params=leaf_call.params,
                        block_number=block_number,
                        block_hash=block_hash,
                    )
                matched_menus = set(threshold_menu_ids)
                for address in involved_addresses:
                    matched_menus.update(watch_map.get(address, {}).keys())

                if not matched_menus:
                    continue

                for monitor_menu_id in matched_menus:
                    profile = profile_map.get(monitor_menu_id)
                    if profile is None:
                        continue

                    matched_aliases = self._collect_aliases(
                        watch_map=watch_map,
                        monitor_menu_id=monitor_menu_id,
                        involved_addresses=involved_addresses,
                    )
                    watched = bool(matched_aliases)
                    above_threshold = (
                        profile.menu_kind == BUILTIN_ALERT_KIND
                        and profile.threshold_tao > 0
                        and amount_tao >= profile.threshold_tao
                    )
                    unresolved_sell = (
                        profile.menu_kind == BUILTIN_ALERT_KIND
                        and action_type in {"stake_remove", "stake_swap"}
                        and amount_tao <= 0
                    )
                    if not watched and not above_threshold and not unresolved_sell:
                        continue

                    primary_from, primary_to = self._pick_primary_route(
                        leaf_call=leaf_call,
                        signer_address=extrinsic_payload["signer_address"],
                        involved_addresses=involved_addresses,
                    )
                    title = ACTION_TITLES.get(action_type, ACTION_TITLES["generic_call"])
                    stable_event_index = extrinsic_index * 1000 + leaf_call_index
                    message = self._build_message(
                        title=title,
                        leaf_call=leaf_call,
                        block_number=block_number,
                        extrinsic_index=extrinsic_index,
                        amount_tao=amount_tao,
                        signer_address=extrinsic_payload["signer_address"],
                        primary_from=primary_from,
                        primary_to=primary_to,
                        involved_addresses=involved_addresses,
                        matched_aliases=matched_aliases,
                        watched=watched,
                        above_threshold=above_threshold,
                        threshold_tao=profile.threshold_tao,
                        success=success,
                        failure_reason=failure_reason,
                        price_estimate=price_estimate,
                        taostats_only=taostats_mode == "only",
                    )
                    raw_payload = {
                        "extrinsic": extrinsic_payload["raw_payload"],
                        "leaf_call": leaf_call.raw_payload,
                        "wrapper_path": leaf_call.wrapper_path,
                        "related_events": [row.payload for row in related_events],
                        "action_type": action_type,
                        "involved_addresses": involved_addresses,
                    }
                    if price_estimate is not None:
                        raw_payload["tao_estimate_status"] = "estimated"
                        raw_payload["tao_estimate"] = {
                            "source": price_estimate.source,
                            "amount_tao": price_estimate.amount_tao,
                            "price_tao_per_alpha": price_estimate.price_tao_per_alpha,
                            "alpha_amount": price_estimate.alpha_amount,
                            "netuid": price_estimate.netuid,
                            "block_number": block_number,
                            "calculated_at": datetime.utcnow().isoformat(),
                        }
                    should_notify = self._should_notify_action(
                        profile=profile,
                        action_type=action_type,
                        watched=watched,
                        above_threshold=above_threshold,
                    )
                    results.append(
                        ActionRecord(
                            monitor_menu_id=monitor_menu_id,
                            owner_user_id=profile.owner_user_id,
                            menu_name=profile.menu_name,
                            block_number=block_number,
                            event_index=stable_event_index,
                            extrinsic_index=extrinsic_index,
                            pallet=leaf_call.pallet,
                            event_name=leaf_call.call_name,
                            action_type=action_type,
                            call_name=leaf_call.call_name,
                            amount_tao=amount_tao,
                            from_address=primary_from,
                            to_address=primary_to,
                            signer_address=extrinsic_payload["signer_address"],
                            extrinsic_hash=extrinsic_payload["extrinsic_hash"],
                            success=success,
                            failure_reason=failure_reason,
                            involved_addresses=involved_addresses,
                            matched_aliases=matched_aliases,
                            message=message,
                            raw_payload=json.dumps(raw_payload, ensure_ascii=False, default=str),
                            should_notify=should_notify,
                            telegram_bot_token=profile.telegram_bot_token,
                            telegram_chat_id=profile.telegram_chat_id,
                        )
                    )

        return results

    def _extract_extrinsic_rows(self, block: Any) -> list[Any]:
        # substrate-interface 返回结构里，extrinsics 通常就在 block.extrinsics 里。
        normalized = self._normalize_value(block)
        if isinstance(normalized, dict):
            rows = normalized.get("extrinsics", [])
            if isinstance(rows, list):
                return rows
        return []

    def _normalize_extrinsic(self, extrinsic: Any, extrinsic_index: int) -> dict[str, Any]:
        # 把一个 extrinsic 统一整理成 signer、hash、call 三部分。
        raw_payload = self._normalize_value(extrinsic)
        if not isinstance(raw_payload, dict):
            raw_payload = {"value": raw_payload}

        signer_address = self._pick_first_address(
            raw_payload.get("address"),
            raw_payload.get("account_id"),
            raw_payload.get("signer"),
            self._dig_value(raw_payload.get("signature"), ("signer", "address")),
        )
        extrinsic_hash = self._pick_string(raw_payload, ("extrinsic_hash", "hash"))
        call_payload = raw_payload.get("call", raw_payload)
        call_envelope = self._normalize_call_payload(
            call_payload=call_payload,
            fallback_pallet=self._pick_string(raw_payload, ("call_module", "module", "pallet")),
            fallback_call=self._pick_string(raw_payload, ("call_function", "function", "name")),
        )
        call_envelope.raw_payload["extrinsic_index"] = extrinsic_index
        return {
            "signer_address": signer_address,
            "extrinsic_hash": extrinsic_hash or None,
            "call": call_envelope,
            "raw_payload": raw_payload,
        }

    def _normalize_call_payload(self, call_payload: Any, fallback_pallet: str = "", fallback_call: str = "") -> CallEnvelope:
        # 一个调用可能来自 extrinsic 根节点，也可能来自 proxy/batch 里的嵌套子调用。
        raw_payload = self._normalize_value(call_payload)
        if not isinstance(raw_payload, dict):
            raw_payload = {"value": raw_payload}

        pallet = self._pick_string(raw_payload, ("call_module", "module", "pallet", "module_id")) or fallback_pallet
        call_name = self._pick_string(raw_payload, ("call_function", "function", "call_name", "name", "event_id")) or fallback_call
        params = self._normalize_call_params(
            raw_payload.get("call_args", raw_payload.get("params", raw_payload.get("args", {})))
        )
        role_addresses = self._extract_named_addresses(params)
        return CallEnvelope(
            pallet=pallet or "Unknown",
            call_name=call_name or "unknown_call",
            params=params,
            wrapper_path=[],
            role_addresses=role_addresses,
            raw_payload=raw_payload,
        )

    def _normalize_call_params(self, params: Any) -> dict[str, Any]:
        # substrate-interface 有时把参数返回成 [{name,value}]，有时直接给 dict。
        normalized = self._normalize_value(params)
        if isinstance(normalized, dict):
            return normalized
        if isinstance(normalized, list):
            if all(isinstance(item, dict) and {"name", "value"}.issubset(item.keys()) for item in normalized):
                return {str(item["name"]): item["value"] for item in normalized}
            return {"items": normalized}
        return {"value": normalized}

    def _normalize_event(self, event: Any, event_index: int) -> EventEnvelope:
        # 统一 event 结构，方便按 extrinsic phase 分组。
        raw_payload = self._normalize_value(getattr(event, "value", event))
        if not isinstance(raw_payload, dict):
            raw_payload = {"value": raw_payload}
        phase = raw_payload.get("phase")
        event_payload = raw_payload.get("event")
        event_source = event_payload if isinstance(event_payload, dict) else raw_payload
        return EventEnvelope(
            event_index=event_index,
            extrinsic_index=self._parse_phase_index(phase),
            pallet=self._pick_string(event_source, ("module_id", "module", "pallet")) or "Unknown",
            event_name=self._pick_string(event_source, ("event_id", "event", "name")) or "unknown_event",
            attributes=event_source.get("attributes", event_source.get("params", {})),
            payload=raw_payload,
        )

    def _group_events_by_extrinsic(self, event_rows: list[EventEnvelope]) -> dict[int, list[EventEnvelope]]:
        # 一个 extrinsic 的成功/失败和实际金额变化，通常都能从它自己 phase 下的 events 里还原。
        grouped: dict[int, list[EventEnvelope]] = {}
        for row in event_rows:
            if row.extrinsic_index is None:
                continue
            grouped.setdefault(row.extrinsic_index, []).append(row)
        return grouped

    def _resolve_success(self, related_events: list[EventEnvelope]) -> tuple[bool, str | None]:
        # system.ExtrinsicSuccess / ExtrinsicFailed 是最可靠的执行状态来源。
        for row in related_events:
            pallet = row.pallet.lower()
            event_name = row.event_name.lower()
            if pallet == "system" and event_name == "extrinsicfailed":
                return False, self._summarize_failure(row.attributes)
        for row in related_events:
            if row.pallet.lower() == "system" and row.event_name.lower() == "extrinsicsuccess":
                return True, None
        return True, None

    def _summarize_failure(self, attributes: Any) -> str | None:
        # 失败原因尽量压缩成一行，便于直接发到 TG。
        normalized = self._normalize_value(attributes)
        if isinstance(normalized, dict):
            module_error = normalized.get("dispatch_error") or normalized.get("error") or normalized
            return json.dumps(module_error, ensure_ascii=False, default=str)
        if isinstance(normalized, list):
            return json.dumps(normalized, ensure_ascii=False, default=str)
        if normalized is None:
            return None
        return str(normalized)

    def _flatten_calls(
        self,
        call_envelope: CallEnvelope,
        signer_address: str | None,
        wrapper_path: list[str],
        inherited_roles: dict[str, str],
    ) -> list[CallEnvelope]:
        # 把 batch/proxy/multisig/sudo/scheduler 这类包装调用递归展开成叶子动作。
        wrapper_label = f"{call_envelope.pallet}.{call_envelope.call_name}"
        current_roles = dict(inherited_roles)
        current_roles.update(call_envelope.role_addresses)
        if signer_address:
            current_roles.setdefault("signer", signer_address)

        nested_calls = self._extract_nested_calls(call_envelope)
        if nested_calls:
            results: list[CallEnvelope] = []
            for nested_call in nested_calls:
                child = self._normalize_call_payload(nested_call)
                results.extend(
                    self._flatten_calls(
                        child,
                        signer_address=signer_address,
                        wrapper_path=wrapper_path + [wrapper_label],
                        inherited_roles=current_roles,
                    )
                )
            if results:
                return results

        return [
            CallEnvelope(
                pallet=call_envelope.pallet,
                call_name=call_envelope.call_name,
                params=call_envelope.params,
                wrapper_path=wrapper_path,
                role_addresses=current_roles,
                raw_payload=call_envelope.raw_payload,
            )
        ]

    def _extract_nested_calls(self, call_envelope: CallEnvelope) -> list[Any]:
        # 针对常见包装器找出里面真正的业务调用。
        pallet = call_envelope.pallet.lower()
        call_name = call_envelope.call_name.lower()
        params = call_envelope.params

        if pallet == "utility" and call_name in {"batch", "batch_all", "force_batch"}:
            calls = params.get("calls", params.get("items", []))
            return calls if isinstance(calls, list) else []

        if (pallet, call_name) in {
            ("proxy", "proxy"),
            ("proxy", "proxy_announced"),
            ("sudo", "sudo"),
            ("sudo", "sudo_unchecked_weight"),
            ("scheduler", "schedule"),
            ("scheduler", "schedule_named"),
            ("multisig", "as_multi"),
            ("multisig", "as_multi_threshold_1"),
        }:
            nested = params.get("call")
            if nested:
                return [nested]
            return []

        return []

    def _build_involved_addresses(
        self,
        leaf_call: CallEnvelope,
        signer_address: str | None,
        related_events: list[EventEnvelope],
    ) -> list[str]:
        # 所有关联地址都从 signer、调用参数和事件结果里汇总，避免钱包只出现在 event 里时漏报。
        addresses: list[str] = []
        if signer_address:
            addresses.append(signer_address)
        addresses.extend(value for value in leaf_call.role_addresses.values() if self._looks_like_address(value))
        addresses.extend(self._collect_addresses(leaf_call.params))
        addresses.extend(self._collect_addresses(leaf_call.raw_payload))
        for event in related_events:
            addresses.extend(self._collect_addresses(event.attributes))
            addresses.extend(self._collect_addresses(event.payload))
        return list(dict.fromkeys(addresses))

    def _estimate_amount_tao(self, leaf_call: CallEnvelope, related_events: list[EventEnvelope]) -> float:
        # 官方动态 TAO 里，减仓/换仓/迁移的调用 amount 多数是 Alpha 数量，不是 TAO。
        # 所以这里只把明确带 tao/rao 语义的字段按 TAO 估值，避免把 Alpha 误报成大额 TAO。
        action_type = self._classify_action_type(leaf_call.pallet, leaf_call.call_name)
        if action_type in {"weights_set", "weights_commit", "weights_reveal", "children_set", "identity_set"}:
            return 0.0

        amount_candidates: list[int] = []
        if action_type == "stake_add":
            # 买入加仓可能在同一个区块或同一个 batch 里出现多笔，优先用当前叶子调用自己的金额拆单。
            amount_candidates.extend(
                self._collect_amount_candidates(
                    leaf_call.params,
                    include_generic_amount=True,
                    include_stake_amount=True,
                )
            )
            if amount_candidates:
                return round(max(amount_candidates) / RAO_PER_TAO, 9)

        amount_candidates.extend(self._collect_settlement_tao_from_events(action_type, related_events))
        amount_candidates.extend(self._collect_balance_tao_from_events(action_type, related_events))
        if action_type == "transfer":
            amount_candidates.extend(self._collect_amount_candidates(leaf_call.params, include_generic_amount=True))
        elif action_type in {"stake_remove", "stake_move", "stake_transfer", "stake_swap", "swap_call"}:
            for event in related_events:
                amount_candidates.extend(self._collect_tao_amount_candidates(event.attributes))
                amount_candidates.extend(self._collect_tao_amount_candidates(event.payload))
        else:
            amount_candidates.extend(self._collect_tao_amount_candidates(leaf_call.params))

        if not amount_candidates:
            return 0.0

        return round(max(amount_candidates) / RAO_PER_TAO, 9)

    def _collect_tao_amount_candidates(self, payload: Any) -> list[int]:
        # 只提取明确写着 TAO/rao/手续费/销毁成本的字段；不吃泛泛的 amount/stake。
        return self._collect_amount_candidates(payload)

    def _collect_settlement_tao_from_events(
        self,
        action_type: str,
        related_events: list[EventEnvelope],
    ) -> list[int]:
        # Subtensor 的质押结算事件很多是按位置编码的，字段名不一定带 tao。
        # 这里按官方事件定义读取 TAO 结算位，避免继续显示“未确认 TAO 成交额”。
        event_tao_index = {
            "stakeadded": 2,
            "stakeremoved": 2,
            "stakemoved": 5,
            "staketransferred": 5,
            "stakeswapped": 4,
        }
        expected_events = {
            "stake_add": {"stakeadded"},
            "stake_remove": {"stakeremoved"},
            "stake_move": {"stakemoved"},
            "stake_transfer": {"staketransferred"},
            "stake_swap": {"stakeswapped"},
        }.get(action_type, set())
        candidates: list[int] = []
        for event in related_events:
            event_name = self._compact_name(event.event_name)
            if expected_events and event_name not in expected_events:
                continue
            event_values = self._event_attribute_values(event.attributes)
            tao_index = event_tao_index.get(event_name)
            if tao_index is not None and len(event_values) > tao_index:
                parsed = self._to_int(event_values[tao_index])
                if parsed is not None and parsed > 0:
                    candidates.append(parsed)
            candidates.extend(self._collect_named_settlement_amounts(event.attributes))
            candidates.extend(self._collect_named_settlement_amounts(event.payload))
            candidates.extend(self._collect_tao_amount_candidates(event.payload))
        return candidates

    def _collect_balance_tao_from_events(
        self,
        action_type: str,
        related_events: list[EventEnvelope],
    ) -> list[int]:
        # 减仓卖出如果 StakeRemoved 格式解析不到，就用同一 extrinsic 里的 TAO 入账事件兜底。
        if action_type not in {"stake_remove", "stake_move", "stake_transfer", "stake_swap"}:
            return []
        balance_amount_index = {
            "deposit": 1,
            "endowed": 1,
            "transfer": 2,
        }
        candidates: list[int] = []
        for event in related_events:
            if event.pallet.lower() != "balances":
                continue
            event_name = self._compact_name(event.event_name)
            amount_index = balance_amount_index.get(event_name)
            if amount_index is None:
                continue
            values = self._event_attribute_values(event.attributes)
            if len(values) > amount_index:
                parsed = self._to_int(values[amount_index])
                if parsed is not None and parsed > 0:
                    candidates.append(parsed)
            candidates.extend(self._collect_named_settlement_amounts(event.attributes))
        return candidates

    def _collect_named_settlement_amounts(self, payload: Any) -> list[int]:
        # 有些节点把 StakeRemoved/StakeSwapped 等事件参数返回成命名 dict，amount 就是官方 TaoBalance。
        normalized = self._normalize_value(payload)
        candidates: list[int] = []
        if isinstance(normalized, dict):
            settlement_keys = {
                "amount",
                "tao_amount",
                "tao",
                "rao",
                "balance",
                "balance_unstaked",
                "balance_staked",
                "balance_moved",
                "balance_transferred",
                "balance_swapped",
                "tao_unstaked",
                "tao_staked",
                "tao_moved",
                "tao_transferred",
                "tao_swapped",
            }
            marker = " ".join(
                str(normalized.get(key, "")).lower()
                for key in ("name", "param", "type", "type_name")
            )
            if (
                "alpha" not in marker
                and ("tao" in marker or "rao" in marker or any(key in marker for key in settlement_keys))
            ):
                parsed = self._to_int(normalized.get("value"))
                if parsed is not None and parsed > 0:
                    candidates.append(parsed)
            for key in settlement_keys:
                parsed = self._to_int(normalized.get(key))
                if parsed is not None and parsed > 0:
                    candidates.append(parsed)
            for key in ("attributes", "params", "args", "data", "values", "event"):
                value = normalized.get(key)
                if isinstance(value, (dict, list)):
                    candidates.extend(self._collect_named_settlement_amounts(value))
            for key, value in normalized.items():
                key_text = str(key).lower()
                if (
                    "alpha" not in key_text
                    and ("tao" in key_text or "rao" in key_text or key_text in settlement_keys)
                ):
                    parsed = self._to_int(value)
                    if parsed is not None and parsed > 0:
                        candidates.append(parsed)
        elif isinstance(normalized, list):
            for item in normalized:
                candidates.extend(self._collect_named_settlement_amounts(item))
        return candidates

    def _event_attribute_values(self, payload: Any) -> list[Any]:
        # substrate-interface 不同版本可能返回 list，也可能返回带 attributes/args 的 dict。
        normalized = self._normalize_value(payload)
        if isinstance(normalized, list):
            values: list[Any] = []
            for item in normalized:
                if isinstance(item, dict) and "value" in item:
                    values.append(item.get("value"))
                else:
                    values.append(item)
            return values
        if isinstance(normalized, dict):
            for key in ("attributes", "args", "data", "values"):
                value = normalized.get(key)
                if isinstance(value, list):
                    return self._event_attribute_values(value)
            if all(str(key).isdigit() for key in normalized):
                return [normalized[key] for key in sorted(normalized, key=lambda item: int(str(item)))]
        return []

    def _collect_amount_candidates(
        self,
        payload: Any,
        *,
        include_generic_amount: bool = False,
        include_stake_amount: bool = False,
    ) -> list[int]:
        # 只提取看起来像金额字段的数值，避免把 netuid、uid、block、alpha、price 误当成 TAO。
        normalized = self._normalize_value(payload)
        candidates: list[int] = []
        tao_keys = ("tao", "rao", "fee", "cost", "burn")
        stake_keys = ("stake", "stake_amount", "amount_staked", "stake_to_be_added")
        generic_keys = ("amount", "value")

        def is_amount_key(key_text: str) -> bool:
            key_text = key_text.lower()
            if any(blocked in key_text for blocked in ("alpha", "price", "netuid", "subnet", "uid", "block")):
                return False
            if any(token in key_text for token in tao_keys):
                return True
            if include_stake_amount and any(token in key_text for token in stake_keys):
                return True
            if include_generic_amount and key_text in generic_keys:
                return True
            return False

        if isinstance(normalized, dict):
            param_name = str(normalized.get("name", normalized.get("param", ""))).lower()
            if is_amount_key(param_name):
                parsed = self._to_int(normalized.get("value"))
                if parsed is not None and parsed > 0:
                    candidates.append(parsed)
            for key, value in normalized.items():
                key_text = str(key).lower()
                if key_text != "value" and is_amount_key(key_text):
                    parsed = self._to_int(value)
                    if parsed is not None and parsed > 0:
                        candidates.append(parsed)
                candidates.extend(
                    self._collect_amount_candidates(
                        value,
                        include_generic_amount=include_generic_amount,
                        include_stake_amount=include_stake_amount,
                    )
                )
        elif isinstance(normalized, list):
            contains_address = any(isinstance(item, str) and self._looks_like_address(item) for item in normalized)
            if include_generic_amount and contains_address:
                for item in normalized:
                    parsed = self._to_int(item)
                    if parsed is not None and parsed > 0:
                        candidates.append(parsed)
            for item in normalized:
                candidates.extend(
                    self._collect_amount_candidates(
                        item,
                        include_generic_amount=include_generic_amount,
                        include_stake_amount=include_stake_amount,
                    )
                )
        return candidates

    def _collect_alpha_amount_candidates(self, payload: Any) -> list[int]:
        # 只用于减仓/换仓展示，避免把 Alpha 数量误写成 TAO 成交额。
        normalized = self._normalize_value(payload)
        candidates: list[int] = []
        ignored_tokens = ("price", "netuid", "subnet", "uid", "block", "hotkey", "coldkey", "delegate", "address")
        amount_tokens = ("alpha", "stake", "amount", "value")

        def is_alpha_amount_key(key_text: str) -> bool:
            key_text = key_text.lower()
            if any(token in key_text for token in ignored_tokens):
                return False
            return any(token in key_text for token in amount_tokens)

        if isinstance(normalized, dict):
            param_name = str(normalized.get("name", normalized.get("param", ""))).lower()
            if is_alpha_amount_key(param_name):
                parsed = self._to_int(normalized.get("value"))
                if parsed is not None and parsed > 0:
                    candidates.append(parsed)
            for key, value in normalized.items():
                key_text = str(key).lower()
                if key_text != "value" and is_alpha_amount_key(key_text):
                    parsed = self._to_int(value)
                    if parsed is not None and parsed > 0:
                        candidates.append(parsed)
                candidates.extend(self._collect_alpha_amount_candidates(value))
        elif isinstance(normalized, list):
            for item in normalized:
                candidates.extend(self._collect_alpha_amount_candidates(item))
        return candidates

    def _collect_limit_price_candidates(self, payload: Any) -> list[int]:
        # TAO 限价通常以 TaoBalance 存在，只用于估算 TAO，不参与数据库阈值判断。
        normalized = self._normalize_value(payload)
        candidates: list[int] = []

        def is_limit_price_key(key_text: str, type_text: str = "") -> bool:
            key_text = key_text.lower()
            type_text = type_text.lower()
            return "limit_price" in key_text or ("price" in key_text and "taobalance" in type_text)

        if isinstance(normalized, dict):
            param_name = str(normalized.get("name", normalized.get("param", "")))
            type_name = str(normalized.get("type", normalized.get("type_name", "")))
            if is_limit_price_key(param_name, type_name):
                parsed = self._to_int(normalized.get("value"))
                if parsed is not None and parsed > 0:
                    candidates.append(parsed)
            for key, value in normalized.items():
                if key != "value" and is_limit_price_key(str(key), type_name):
                    parsed = self._to_int(value)
                    if parsed is not None and parsed > 0:
                        candidates.append(parsed)
                candidates.extend(self._collect_limit_price_candidates(value))
        elif isinstance(normalized, list):
            for item in normalized:
                candidates.extend(self._collect_limit_price_candidates(item))
        return candidates

    def _classify_action_type(self, pallet: str, call_name: str) -> str:
        # 把当前 runtime 的常见 TAO 生态调用归到统一业务动作名称上。
        pallet_lower = pallet.lower()
        call_lower = call_name.lower()

        if pallet_lower == "balances" and call_lower.startswith("transfer"):
            return "transfer"
        if "add_stake" in call_lower:
            return "stake_add"
        if call_lower in {"unstake", "unstake_all"} or "remove_stake" in call_lower:
            return "stake_remove"
        if "move_stake" in call_lower:
            return "stake_move"
        if "transfer_stake" in call_lower:
            return "stake_transfer"
        if "swap_stake" in call_lower:
            return "stake_swap"
        if "delegate" in call_lower:
            return "delegate_change"
        if "root_register" in call_lower:
            return "root_register"
        if "burned_register" in call_lower:
            return "burned_register"
        if call_lower in {"register", "register_network", "register_subnet"}:
            return "subnet_register"
        if "subnet" in call_lower or "network" in call_lower or call_lower.startswith("start_"):
            return "subnet_manage"
        if "set_weights" in call_lower:
            return "weights_set"
        if "commit_weights" in call_lower:
            return "weights_commit"
        if "reveal_weights" in call_lower:
            return "weights_reveal"
        if "serve_axon" in call_lower or "serve_prometheus" in call_lower:
            return "axon_serve"
        if "set_children" in call_lower:
            return "children_set"
        if "identity" in call_lower:
            return "identity_set"
        if "liquidity" in call_lower:
            return "liquidity_manage"
        if pallet_lower == "swap" or "swap" in call_lower:
            return "swap_call"
        if pallet_lower == "registry":
            return "registry_call"
        if pallet_lower == "commitments":
            return "commitment_call"
        if pallet_lower == "proxy":
            return "proxy_call"
        if pallet_lower == "multisig":
            return "multisig_call"
        if pallet_lower == "utility":
            return "utility_batch"
        if pallet_lower in {"ethereum", "evm"}:
            return "evm_transact"
        if pallet_lower == "mevshield":
            return "shielded_call"
        return "generic_call"

    def _should_ignore_action(self, action_type: str) -> bool:
        # 当前项目主要服务 TAO 交易，默认忽略 EVM 噪音；后续如有需要再做成开关。
        return action_type in IGNORED_ACTION_TYPES

    def _compact_name(self, value: str) -> str:
        # 不同 RPC/库可能返回 StakeRemoved、stake_removed 或 stake-removed，统一后再匹配。
        return re.sub(r"[^a-z0-9]", "", str(value).lower())

    def _is_trade_action(self, action_type: str) -> bool:
        # 失败的交易调用没有真实成交，不能进入短线交易监控结果。
        return action_type in {
            "transfer",
            "stake_add",
            "stake_remove",
            "stake_move",
            "stake_transfer",
            "stake_swap",
            "swap_call",
        }

    def _should_notify_action(
        self,
        profile: NotificationProfile,
        action_type: str,
        watched: bool,
        above_threshold: bool,
    ) -> bool:
        # 监控钱包命中一定推 TG；其他交易暂时只入库，不主动推送。
        if not profile.telegram_bot_token or not profile.telegram_chat_id:
            return False
        if watched:
            return True
        return False

    def _collect_aliases(
        self,
        watch_map: dict[str, dict[int, list[str]]],
        monitor_menu_id: int,
        involved_addresses: list[str],
    ) -> list[str]:
        # 同一监控菜单可能监控了多个关联地址，所以这里做一次去重汇总。
        aliases: list[str] = []
        for address in involved_addresses:
            aliases.extend(watch_map.get(address, {}).get(monitor_menu_id, []))
        return list(dict.fromkeys(aliases))

    def _pick_primary_route(
        self,
        leaf_call: CallEnvelope,
        signer_address: str | None,
        involved_addresses: list[str],
    ) -> tuple[str | None, str | None]:
        # 页面主表只显示一条“路径”，这里尽量挑出最像 from/to 的两个地址。
        from_address = self._pick_first_address(
            leaf_call.role_addresses.get("from"),
            leaf_call.role_addresses.get("coldkey"),
            leaf_call.role_addresses.get("source"),
            leaf_call.role_addresses.get("real"),
            signer_address,
        )
        to_address = self._pick_first_address(
            leaf_call.role_addresses.get("to"),
            leaf_call.role_addresses.get("dest"),
            leaf_call.role_addresses.get("destination"),
            leaf_call.role_addresses.get("hotkey"),
            leaf_call.role_addresses.get("delegate"),
            leaf_call.role_addresses.get("owner"),
        )

        if from_address and to_address and from_address != to_address:
            return from_address, to_address
        if len(involved_addresses) >= 2:
            return involved_addresses[0], involved_addresses[1]
        if len(involved_addresses) == 1:
            return involved_addresses[0], None
        return from_address, to_address

    def _build_message(
        self,
        title: str,
        leaf_call: CallEnvelope,
        block_number: int,
        extrinsic_index: int,
        amount_tao: float,
        signer_address: str | None,
        primary_from: str | None,
        primary_to: str | None,
        involved_addresses: list[str],
        matched_aliases: list[str],
        watched: bool,
        above_threshold: bool,
        threshold_tao: float,
        success: bool,
        failure_reason: str | None,
        price_estimate: TaoPriceEstimate | None = None,
        taostats_only: bool = False,
    ) -> str:
        # 消息内容直接面向 TG 和网页弹窗，所以把调用、状态、关联地址和命中原因都写清楚。
        action_type = self._classify_action_type(leaf_call.pallet, leaf_call.call_name)
        signal = self._build_trade_signal(
            action_type=action_type,
            amount_tao=amount_tao,
            params=leaf_call.params,
        )
        amount_label = f"{amount_tao:.6f} TAO"
        if amount_tao <= 0 and action_type in {"stake_remove", "stake_move", "stake_transfer", "stake_swap", "swap_call"}:
            if taostats_only:
                amount_label = "未确认 TAO 成交额（等待 TaoStats）"
            elif price_estimate is not None:
                amount_label = f"约 {price_estimate.amount_tao:.6f} TAO（按子网价格估算）"
            else:
                estimated_tao = self._estimate_limit_price_tao(leaf_call.params)
                if estimated_tao > 0:
                    amount_label = f"约 {estimated_tao:.6f} TAO（按限价估算）"
                else:
                    amount_label = "未确认 TAO 成交额（链上参数多为 Alpha 数量）"
        tags: list[str] = []
        if watched:
            tags.append(f"监控钱包: {', '.join(matched_aliases)}")
        if above_threshold:
            tags.append(f"大额阈值: >= {threshold_tao} TAO")
        if leaf_call.wrapper_path:
            tags.append(f"封装路径: {' -> '.join(leaf_call.wrapper_path)}")

        title_prefix = self._telegram_title_prefix(action_type)
        lines = [
            f"<b>{title_prefix}{title}</b>",
            f"状态: <b>{'成功' if success else '失败'}</b>",
            f"调用: <code>{leaf_call.pallet}.{leaf_call.call_name}</code>",
            f"子网: <b>{signal['subnet_label']}</b>",
            f"方向: <b>{signal['direction']}</b>",
            f"信号: <b>{signal['signal']}</b>",
            f"区块: <code>{block_number}</code>",
            f"Extrinsic: <code>{extrinsic_index}</code>",
            f"签名者: <code>{signer_address or '-'}</code>",
            f"金额估值: <b>{amount_label}</b>",
            f"主路径: <code>{primary_from or '-'} -> {primary_to or '-'}</code>",
            f"关联地址: <code>{', '.join(involved_addresses[:8]) if involved_addresses else '-'}</code>",
        ]
        if tags:
            lines.append(f"命中原因: {', '.join(tags)}")
        if failure_reason:
            lines.append(f"失败原因: <code>{failure_reason}</code>")
        return "\n".join(lines)

    def _telegram_title_prefix(self, action_type: str) -> str:
        # TG 标题最前面放交易方向符号，方便手机通知里一眼区分买卖。
        if action_type == "stake_add":
            return "🟢 "
        if action_type == "stake_remove":
            return "🔴 "
        if action_type in {"stake_move", "stake_transfer", "stake_swap", "swap_call"}:
            return "🟡 "
        return ""

    def _build_trade_signal(self, action_type: str, amount_tao: float, params: Any) -> dict[str, str]:
        # 把底层链上动作翻译成更接近短线交易判断的中文信号。
        subnet_ids = self._extract_subnet_ids(params)
        subnet_label = self._subnet_label_for_action(action_type, subnet_ids)
        direction_map = {
            "stake_add": "买入 / 加仓",
            "stake_remove": "卖出 / 减仓",
            "stake_move": "迁移仓位",
            "stake_transfer": "转移仓位",
            "stake_swap": "换仓",
            "swap_call": "兑换",
            "liquidity_manage": "流动性操作",
            "subnet_register": "子网注册",
            "subnet_manage": "子网管理",
            "transfer": "资金转移",
        }
        direction = direction_map.get(action_type, "链上动作")

        if action_type in {"stake_add", "stake_remove", "stake_move", "stake_transfer", "stake_swap", "swap_call"}:
            if amount_tao >= 100:
                signal = f"大额{direction}"
            elif amount_tao >= 10:
                signal = f"中额{direction}"
            elif amount_tao > 0:
                signal = f"小额{direction}"
            else:
                signal = direction
        elif action_type == "transfer" and amount_tao >= 100:
            signal = "大额资金转移"
        else:
            signal = direction

        return {
            "subnet_label": subnet_label,
            "direction": direction,
            "signal": signal,
        }

    def _estimate_alpha_amount(self, payload: Any) -> float:
        # 减仓/换仓在调用参数里通常给的是 Alpha 数量；只做展示兜底，不参与 TAO 阈值判断。
        candidates = self._collect_alpha_amount_candidates(payload)
        if not candidates:
            return 0.0
        return round(max(candidates) / RAO_PER_TAO, 9)

    def _estimate_limit_price_tao(self, payload: Any) -> float:
        # 没有成交事件时，用 Alpha 数量乘以 limit_price 得到 TAO 估算值，仍不参与阈值判断。
        alpha_candidates = self._collect_alpha_amount_candidates(payload)
        price_candidates = self._collect_limit_price_candidates(payload)
        if not alpha_candidates or not price_candidates:
            return 0.0
        return round((max(alpha_candidates) * max(price_candidates)) / RAO_PER_TAO / RAO_PER_TAO, 9)

    def _subnet_label_for_action(self, action_type: str, subnet_ids: list[int]) -> str:
        # 余额普通转账本身不带子网字段，显示“无子网字段”比“未知”更准确。
        if subnet_ids:
            return "、".join(f"子网 {netuid}" for netuid in subnet_ids[:3])
        if action_type == "transfer":
            return "无子网字段"
        return "未知"

    def _extract_subnet_ids(self, payload: Any) -> list[int]:
        # 常见字段包括 netuid、subnet、network，递归提取后去重。
        normalized = self._normalize_value(payload)
        results: list[int] = []
        subnet_keys = ("netuid", "subnet", "network", "destination_netuid", "origin_netuid")

        if isinstance(normalized, dict):
            param_name = str(normalized.get("name", normalized.get("param", ""))).lower()
            if any(token in param_name for token in subnet_keys):
                parsed = self._to_int(normalized.get("value"))
                if parsed is not None and 0 <= parsed <= 10_000:
                    results.append(parsed)
            for key, value in normalized.items():
                key_text = str(key).lower()
                if any(token in key_text for token in subnet_keys):
                    parsed = self._to_int(value)
                    if parsed is not None and 0 <= parsed <= 10_000:
                        results.append(parsed)
                results.extend(self._extract_subnet_ids(value))
        elif isinstance(normalized, list):
            for item in normalized:
                results.extend(self._extract_subnet_ids(item))

        return list(dict.fromkeys(results))

    async def _persist_and_notify(self, actions: list[ActionRecord]) -> None:
        # 先按账号入库去重，再写入 Telegram 队列；实际发送由独立 worker 负责。
        if not actions:
            return

        for action in self._dedupe_actions_for_owner(actions):
            with session_scope() as session:
                exists = self._find_existing_event(session, action)
                if exists:
                    self._refresh_existing_event(exists, action)
                    if action.should_notify and not bool(exists.notification_sent):
                        self._enqueue_notification(session, exists, action)
                else:
                    row = ChainEvent(
                        owner_user_id=action.owner_user_id,
                        monitor_menu_id=action.monitor_menu_id,
                        block_number=action.block_number,
                        event_index=action.event_index,
                        extrinsic_index=action.extrinsic_index,
                        pallet=action.pallet,
                        event_name=action.event_name,
                        action_type=action.action_type,
                        call_name=action.call_name,
                        amount_tao=action.amount_tao,
                        from_address=action.from_address,
                        to_address=action.to_address,
                        signer_address=action.signer_address,
                        extrinsic_hash=action.extrinsic_hash,
                        success=action.success,
                        failure_reason=action.failure_reason,
                        involved_addresses_json=json.dumps(action.involved_addresses, ensure_ascii=False),
                        matched_aliases_json=json.dumps(action.matched_aliases, ensure_ascii=False),
                        message=action.message,
                        raw_payload=action.raw_payload,
                        notification_sent=False,
                    )
                    session.add(row)
                    session.flush()
                    if action.should_notify:
                        self._enqueue_notification(session, row, action)

    def _enqueue_notification(self, session: Any, event: ChainEvent, action: ActionRecord) -> None:
        # 同一条链上命中只保留一条未完成通知，避免快速监听和 finalized 校正重复推送。
        existing = session.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.chain_event_id == event.id,
                NotificationOutbox.status.in_(("pending", "retrying", "sending", "sent", "failed")),
            )
        )
        if existing is not None:
            return
        session.add(
            NotificationOutbox(
                owner_user_id=action.owner_user_id,
                monitor_menu_id=action.monitor_menu_id,
                chain_event_id=event.id,
                telegram_bot_token=action.telegram_bot_token,
                telegram_chat_id=action.telegram_chat_id,
                message=action.message,
                status="pending",
                attempts=0,
                max_attempts=10,
                next_retry_at=datetime.utcnow(),
                last_error="",
            )
        )

    def _refresh_existing_event(self, row: ChainEvent, action: ActionRecord) -> None:
        # 首见监听可能先入库一个没有 events 的版本；finalized 校正扫到完整数据后要能补全金额和原始 payload。
        existing_raw = self._safe_json_loads(row.raw_payload)
        incoming_raw = self._safe_json_loads(action.raw_payload)
        existing_events = self._payload_event_count(existing_raw)
        incoming_events = self._payload_event_count(incoming_raw)

        row.pallet = action.pallet
        row.event_name = action.event_name
        row.action_type = action.action_type
        row.call_name = action.call_name
        row.from_address = action.from_address
        row.to_address = action.to_address
        row.signer_address = action.signer_address
        row.extrinsic_hash = action.extrinsic_hash
        row.success = action.success
        row.failure_reason = action.failure_reason
        row.involved_addresses_json = json.dumps(action.involved_addresses, ensure_ascii=False)
        row.matched_aliases_json = json.dumps(action.matched_aliases, ensure_ascii=False)

        if action.amount_tao > 0 or row.amount_tao <= 0:
            row.amount_tao = action.amount_tao
        if incoming_events >= existing_events:
            row.raw_payload = action.raw_payload
        if action.amount_tao > 0 or incoming_events > existing_events or not row.message:
            row.message = action.message

    def _safe_json_loads(self, payload: str) -> Any:
        try:
            return json.loads(payload or "{}")
        except Exception:
            return {}

    def _payload_event_count(self, payload: Any) -> int:
        if not isinstance(payload, dict):
            return 0
        related_events = payload.get("related_events")
        return len(related_events) if isinstance(related_events, list) else 0

    def _dedupe_actions_for_owner(self, actions: list[ActionRecord]) -> list[ActionRecord]:
        # 同一笔链上动作可能同时命中“大额预警”和“钱包监控”，同一账号只保留一条，避免页面和 TG 重复。
        deduped: dict[tuple[Any, ...], ActionRecord] = {}
        for action in actions:
            key = self._owner_event_key(action)
            current = deduped.get(key)
            if current is None or self._action_priority(action) > self._action_priority(current):
                deduped[key] = action
        return list(deduped.values())

    def _owner_event_key(self, action: ActionRecord) -> tuple[Any, ...]:
        return (
            action.owner_user_id,
            action.block_number,
            action.extrinsic_index,
            action.event_index,
            action.action_type,
            action.call_name,
            action.amount_tao,
            action.signer_address,
            action.from_address,
            action.to_address,
        )

    def _action_priority(self, action: ActionRecord) -> int:
        # 钱包命中的信息更有上下文，其次是有 TG 的大额预警，最后只是普通入库。
        if action.should_notify:
            return 40 if action.matched_aliases else 30
        if action.matched_aliases:
            return 20
        return 10

    def _find_existing_event(self, session: Any, action: ActionRecord) -> ChainEvent | None:
        # 快速监听和 finalized 校正会重复扫描同一个区块；这里用稳定身份防止重复入库和重复 TG。
        exact = session.scalar(
            select(ChainEvent).where(
                ChainEvent.monitor_menu_id == action.monitor_menu_id,
                ChainEvent.block_number == action.block_number,
                ChainEvent.event_index == action.event_index,
            )
        )
        if exact:
            return exact

        same_menu = session.scalar(
            select(ChainEvent).where(
                ChainEvent.monitor_menu_id == action.monitor_menu_id,
                ChainEvent.block_number == action.block_number,
                ChainEvent.extrinsic_index == action.extrinsic_index,
                ChainEvent.action_type == action.action_type,
                ChainEvent.call_name == action.call_name,
                ChainEvent.amount_tao == action.amount_tao,
                ChainEvent.signer_address == action.signer_address,
                ChainEvent.from_address == action.from_address,
                ChainEvent.to_address == action.to_address,
            )
        )
        if same_menu:
            return same_menu

        return session.scalar(
            select(ChainEvent).where(
                ChainEvent.owner_user_id == action.owner_user_id,
                ChainEvent.block_number == action.block_number,
                ChainEvent.extrinsic_index == action.extrinsic_index,
                ChainEvent.event_index == action.event_index,
                ChainEvent.action_type == action.action_type,
                ChainEvent.call_name == action.call_name,
                ChainEvent.amount_tao == action.amount_tao,
                ChainEvent.signer_address == action.signer_address,
                ChainEvent.from_address == action.from_address,
                ChainEvent.to_address == action.to_address,
            )
        )

    def _extract_named_addresses(self, payload: Any) -> dict[str, str]:
        # 从常见命名参数里提取地址角色，便于后面做 wallet 命中和路径展示。
        normalized = self._normalize_value(payload)
        collected: dict[str, str] = {}
        if isinstance(normalized, dict):
            for key, value in normalized.items():
                key_text = str(key).lower()
                if any(
                    token in key_text
                    for token in (
                        "address",
                        "dest",
                        "destination",
                        "source",
                        "from",
                        "to",
                        "who",
                        "coldkey",
                        "hotkey",
                        "owner",
                        "delegate",
                        "real",
                        "proxy",
                        "signer",
                        "child",
                        "parent",
                    )
                ):
                    address = self._pick_first_address(value)
                    if address:
                        collected[key_text] = address
                child_values = self._extract_named_addresses(value)
                for child_key, child_value in child_values.items():
                    collected.setdefault(child_key, child_value)
        elif isinstance(normalized, list):
            for item in normalized:
                child_values = self._extract_named_addresses(item)
                for child_key, child_value in child_values.items():
                    collected.setdefault(child_key, child_value)
        return collected

    def _collect_addresses(self, payload: Any) -> list[str]:
        # 对整个参数树做一遍递归扫描，尽量把所有参与地址都找出来。
        normalized = self._normalize_value(payload)
        results: list[str] = []
        if isinstance(normalized, dict):
            for value in normalized.values():
                results.extend(self._collect_addresses(value))
        elif isinstance(normalized, list):
            for item in normalized:
                results.extend(self._collect_addresses(item))
        elif isinstance(normalized, str) and self._looks_like_address(normalized):
            results.append(normalized)
        return list(dict.fromkeys(results))

    def _parse_phase_index(self, phase: Any) -> int | None:
        # event.phase 一般是 {"ApplyExtrinsic": n}，这里兼容几种常见表示法。
        normalized = self._normalize_value(phase)
        if isinstance(normalized, dict):
            candidate = normalized.get("ApplyExtrinsic")
            if candidate is None:
                candidate = normalized.get("apply_extrinsic")
            return self._to_int(candidate)
        if isinstance(normalized, str):
            match = re.search(r"(\d+)", normalized)
            if match:
                return int(match.group(1))
        return None

    def _get_latest_finalized_block(self, substrate: SubstrateInterface) -> int:
        # finalized head 和 block number 都是同步 RPC，这里放在线程里统一执行。
        finalized_head = substrate.get_chain_finalised_head()
        return int(substrate.get_block_number(finalized_head))

    def _get_latest_head_block(self, substrate: SubstrateInterface) -> int:
        # 最新块头比最终确认块更早，用来做首见提醒；失败时退回最终确认块。
        try:
            try:
                chain_head = substrate.get_chain_head()
            except AttributeError:
                response = substrate.rpc_request("chain_getHead", [])
                chain_head = response.get("result") if isinstance(response, dict) else response
            return int(substrate.get_block_number(chain_head))
        except Exception:
            logger.exception("读取最新块头失败，退回使用最终确认块")
            return self._get_latest_finalized_block(substrate)

    def _header_block_number(self, payload: Any) -> int:
        # 新块头订阅返回的结构在不同版本里略有差异，这里统一提取区块高度。
        normalized = self._normalize_value(payload)
        if isinstance(normalized, dict):
            candidates = (
                normalized.get("number"),
                normalized.get("block_number"),
                normalized.get("header", {}).get("number") if isinstance(normalized.get("header"), dict) else None,
                normalized.get("params", {}).get("result", {}).get("number")
                if isinstance(normalized.get("params"), dict) and isinstance(normalized.get("params", {}).get("result"), dict)
                else None,
            )
            for candidate in candidates:
                number = self._to_int(candidate)
                if number is not None:
                    return number
        number = self._to_int(normalized)
        return int(number or 0)

    def _normalize_value(self, value: Any) -> Any:
        # 尽量把 substrate-interface 的对象递归转换成普通 Python 数据。
        if isinstance(value, dict):
            return {str(key): self._normalize_value(val) for key, val in value.items()}
        if isinstance(value, list):
            return [self._normalize_value(item) for item in value]
        if isinstance(value, tuple):
            return [self._normalize_value(item) for item in value]
        if hasattr(value, "to_dict"):
            try:
                converted = value.to_dict()
            except Exception:
                converted = None
            if converted is not None:
                return self._normalize_value(converted)
        if hasattr(value, "value") and not isinstance(value, (str, bytes, int, float, bool)):
            inner = getattr(value, "value")
            if inner is not value:
                return self._normalize_value(inner)
        if hasattr(value, "__dict__") and not isinstance(value, (str, bytes, int, float, bool)):
            return {
                str(key): self._normalize_value(val)
                for key, val in value.__dict__.items()
                if not str(key).startswith("_")
            }
        return value

    def _pick_string(self, payload: dict[str, Any], keys: tuple[str, ...]) -> str:
        # 某些字段名在不同链版本里会变化，这里按候选顺序取值。
        for key in keys:
            value = payload.get(key)
            if value is not None:
                return str(value)
        return ""

    def _dig_value(self, payload: Any, keys: tuple[str, ...]) -> Any:
        # 某些对象会把 signer 包在 signature/address 里，这里做一层安全读取。
        normalized = self._normalize_value(payload)
        if not isinstance(normalized, dict):
            return None
        for key in keys:
            if key in normalized:
                return normalized[key]
        return None

    def _pick_first_address(self, *values: Any) -> str | None:
        # 从多个候选值里拿到第一个看起来像地址的字符串。
        for value in values:
            normalized = self._normalize_value(value)
            if isinstance(normalized, list):
                for item in normalized:
                    found = self._pick_first_address(item)
                    if found:
                        return found
                continue
            if isinstance(normalized, str) and self._looks_like_address(normalized):
                return normalized
        return None

    def _looks_like_address(self, value: str) -> bool:
        # 兼容 SS58 地址和 0x 地址。
        return bool(SS58_PATTERN.match(value) or HEX_PATTERN.match(value))

    def _to_int(self, value: Any) -> int | None:
        # 链上金额既可能是整数，也可能是纯数字字符串。
        normalized = self._normalize_value(value)
        if normalized is None:
            return None
        if isinstance(normalized, bool):
            return None
        if isinstance(normalized, int):
            return normalized
        if isinstance(normalized, float) and math.isfinite(normalized):
            return int(normalized)
        if isinstance(normalized, dict):
            for key in ("value", "amount", "balance", "tao", "rao", "bits", "compact"):
                if key in normalized:
                    parsed = self._to_int(normalized.get(key))
                    if parsed is not None:
                        return parsed
            return None
        if isinstance(normalized, list) and len(normalized) == 1:
            return self._to_int(normalized[0])
        if isinstance(normalized, str):
            digits = normalized.replace(",", "").replace("_", "")
            if digits.isdigit():
                return int(digits)
            if digits.lower().startswith("0x"):
                with suppress(ValueError):
                    return int(digits, 16)
        return None


def ensure_state(session) -> MonitorState:
    # 监听状态表只存一行，不存在时自动创建。
    state = session.get(MonitorState, 1)
    if state is None:
        state = MonitorState(id=1, monitor_status="idle", last_scanned_block=0, last_seen_head=0)
        session.add(state)
        session.flush()
    return state

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from substrateinterface import SubstrateInterface

from app.database import session_scope
from app.models import ChainEvent, MonitorState, UserSetting, WalletWatch
from app.services.settings_service import get_system_runtime_settings, typed_system_runtime_settings
from app.services.telegram import TelegramNotifier


logger = logging.getLogger(__name__)
# TAO 和 Rao 的换算常量。
RAO_PER_TAO = 1_000_000_000
SS58_PATTERN = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{40,80}$")
HEX_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40,66}$")

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
    owner_user_id: int
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
    owner_user_id: int
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


class SubtensorMonitor:
    def __init__(self) -> None:
        # 监听任务会在 FastAPI 生命周期内启动和关闭。
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._wakeup_event = asyncio.Event()
        self._notifier = TelegramNotifier()

    async def start(self) -> None:
        # 避免重复创建监听任务。
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="subtensor-monitor")

    async def stop(self) -> None:
        # 优雅停止后台扫描任务。
        self._stop_event.set()
        self._wakeup_event.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def restart(self) -> None:
        # 配置保存后唤醒当前循环，让新设置尽快生效。
        self._wakeup_event.set()

    async def _run(self) -> None:
        # 后台常驻循环：扫描链、记录错误、按配置间隔休眠。
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
        # 链路级扫描间隔属于系统配置，只需读取一次总管理员维护的设置。
        with session_scope() as session:
            raw = get_system_runtime_settings(session)
        settings = typed_system_runtime_settings(raw)
        return int(settings["poll_interval_seconds"])

    async def _scan_once(self) -> None:
        # 单轮扫描：读取系统设置、账号配置、钱包列表，再逐块解码全部调用动作。
        with session_scope() as session:
            raw_settings = get_system_runtime_settings(session)
            typed = typed_system_runtime_settings(raw_settings)
            state = ensure_state(session)
            wallet_rows = session.scalars(select(WalletWatch).where(WalletWatch.enabled.is_(True))).all()
            user_setting_rows = session.scalars(select(UserSetting)).all()
            watch_map = self._build_watch_map(wallet_rows)
            profile_map = {
                row.owner_user_id: NotificationProfile(
                    owner_user_id=row.owner_user_id,
                    threshold_tao=float(row.large_transfer_threshold_tao),
                    telegram_bot_token=row.telegram_bot_token,
                    telegram_chat_id=row.telegram_chat_id,
                )
                for row in user_setting_rows
            }
            state.monitor_status = "running"
            state.last_error = None

        substrate = SubstrateInterface(url=str(typed["subtensor_ws_url"]))
        latest_block = await asyncio.to_thread(self._get_latest_finalized_block, substrate)
        target_block = max(0, int(latest_block) - int(typed["finality_lag_blocks"]))

        with session_scope() as session:
            state = ensure_state(session)
            start_block = state.last_scanned_block + 1 if state.last_scanned_block else max(target_block - 20, 1)
            state.last_seen_head = int(latest_block)

        if start_block > target_block:
            return

        for block_number in range(start_block, target_block + 1):
            actions = await asyncio.to_thread(
                self._extract_actions_sync,
                substrate,
                block_number,
                watch_map,
                profile_map,
            )
            await self._persist_and_notify(actions)
            with session_scope() as session:
                state = ensure_state(session)
                state.last_scanned_block = block_number
                state.updated_at = datetime.utcnow()

    def _build_watch_map(self, wallet_rows: list[WalletWatch]) -> dict[str, dict[int, list[str]]]:
        # 同一个地址允许被多个账号分别监控，所以用 address -> user_id -> aliases 的结构。
        watch_map: dict[str, dict[int, list[str]]] = {}
        for row in wallet_rows:
            watch_map.setdefault(row.address, {}).setdefault(row.owner_user_id, []).append(row.alias)
        return watch_map

    def _extract_actions_sync(
        self,
        substrate: SubstrateInterface,
        block_number: int,
        watch_map: dict[str, dict[int, list[str]]],
        profile_map: dict[int, NotificationProfile],
    ) -> list[ActionRecord]:
        # 每个区块都读取全部 extrinsic 和 event，再展开成统一动作。
        block_hash = substrate.get_block_hash(block_number)
        block = substrate.get_block(block_hash=block_hash)
        events = substrate.get_events(block_hash=block_hash)

        extrinsic_rows = self._extract_extrinsic_rows(block)
        event_rows = [self._normalize_event(event, idx) for idx, event in enumerate(events)]
        events_by_extrinsic = self._group_events_by_extrinsic(event_rows)

        threshold_user_ids = {
            owner_user_id for owner_user_id, profile in profile_map.items() if profile.threshold_tao > 0
        }

        results: list[ActionRecord] = []
        block_action_index = 0

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

            for leaf_call in leaf_calls:
                involved_addresses = self._build_involved_addresses(leaf_call, extrinsic_payload["signer_address"])
                amount_tao = self._estimate_amount_tao(leaf_call, related_events)
                action_type = self._classify_action_type(leaf_call.pallet, leaf_call.call_name)
                matched_users = set(threshold_user_ids)
                for address in involved_addresses:
                    matched_users.update(watch_map.get(address, {}).keys())

                if not matched_users:
                    continue

                for owner_user_id in matched_users:
                    profile = profile_map.get(owner_user_id)
                    if profile is None:
                        continue

                    matched_aliases = self._collect_aliases(
                        watch_map=watch_map,
                        owner_user_id=owner_user_id,
                        involved_addresses=involved_addresses,
                    )
                    watched = bool(matched_aliases)
                    above_threshold = profile.threshold_tao > 0 and amount_tao >= profile.threshold_tao
                    if not watched and not above_threshold:
                        continue

                    primary_from, primary_to = self._pick_primary_route(
                        leaf_call=leaf_call,
                        signer_address=extrinsic_payload["signer_address"],
                        involved_addresses=involved_addresses,
                    )
                    title = ACTION_TITLES.get(action_type, ACTION_TITLES["generic_call"])
                    block_action_index += 1
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
                    )
                    raw_payload = {
                        "extrinsic": extrinsic_payload["raw_payload"],
                        "leaf_call": leaf_call.raw_payload,
                        "wrapper_path": leaf_call.wrapper_path,
                        "related_events": [row.payload for row in related_events],
                        "action_type": action_type,
                        "involved_addresses": involved_addresses,
                    }
                    results.append(
                        ActionRecord(
                            owner_user_id=owner_user_id,
                            block_number=block_number,
                            event_index=block_action_index,
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
                            should_notify=bool(profile.telegram_bot_token and profile.telegram_chat_id),
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
        return EventEnvelope(
            event_index=event_index,
            extrinsic_index=self._parse_phase_index(phase),
            pallet=self._pick_string(raw_payload, ("module_id", "module", "pallet")) or "Unknown",
            event_name=self._pick_string(raw_payload, ("event_id", "event", "name")) or "unknown_event",
            attributes=raw_payload.get("attributes", raw_payload.get("params", {})),
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

    def _build_involved_addresses(self, leaf_call: CallEnvelope, signer_address: str | None) -> list[str]:
        # 所有关联地址都从 signer、显式命名参数和递归扫描结果里汇总。
        addresses: list[str] = []
        if signer_address:
            addresses.append(signer_address)
        addresses.extend(value for value in leaf_call.role_addresses.values() if self._looks_like_address(value))
        addresses.extend(self._collect_addresses(leaf_call.params))
        return list(dict.fromkeys(addresses))

    def _estimate_amount_tao(self, leaf_call: CallEnvelope, related_events: list[EventEnvelope]) -> float:
        # 尽量从调用参数和关联 events 中提取经济量，统一按 1e9 精度折算为 TAO 数字。
        action_type = self._classify_action_type(leaf_call.pallet, leaf_call.call_name)
        if action_type in {"weights_set", "weights_commit", "weights_reveal", "children_set", "identity_set"}:
            return 0.0

        amount_candidates: list[int] = []
        amount_candidates.extend(self._collect_amount_candidates(leaf_call.params))
        for event in related_events:
            amount_candidates.extend(self._collect_amount_candidates(event.attributes))

        if not amount_candidates:
            return 0.0

        return round(max(amount_candidates) / RAO_PER_TAO, 9)

    def _collect_amount_candidates(self, payload: Any) -> list[int]:
        # 只提取看起来像金额字段的数值，避免把 netuid、uid、block 等误当成 TAO。
        normalized = self._normalize_value(payload)
        candidates: list[int] = []
        amountish_keys = (
            "amount",
            "value",
            "stake",
            "stake_amount",
            "tao",
            "tao_amount",
            "alpha",
            "alpha_amount",
            "burn",
            "fee",
            "cost",
            "price",
        )

        if isinstance(normalized, dict):
            for key, value in normalized.items():
                key_text = str(key).lower()
                if any(token in key_text for token in amountish_keys):
                    parsed = self._to_int(value)
                    if parsed is not None and parsed > 0:
                        candidates.append(parsed)
                candidates.extend(self._collect_amount_candidates(value))
        elif isinstance(normalized, list):
            contains_address = any(isinstance(item, str) and self._looks_like_address(item) for item in normalized)
            if contains_address:
                for item in normalized:
                    parsed = self._to_int(item)
                    if parsed is not None and parsed > 0:
                        candidates.append(parsed)
            for item in normalized:
                candidates.extend(self._collect_amount_candidates(item))
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

    def _collect_aliases(
        self,
        watch_map: dict[str, dict[int, list[str]]],
        owner_user_id: int,
        involved_addresses: list[str],
    ) -> list[str]:
        # 同一账号可能监控了多个关联地址，所以这里做一次去重汇总。
        aliases: list[str] = []
        for address in involved_addresses:
            aliases.extend(watch_map.get(address, {}).get(owner_user_id, []))
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
    ) -> str:
        # 消息内容直接面向 TG 和网页弹窗，所以把调用、状态、关联地址和命中原因都写清楚。
        tags: list[str] = []
        if watched:
            tags.append(f"监控钱包: {', '.join(matched_aliases)}")
        if above_threshold:
            tags.append(f"大额阈值: >= {threshold_tao} TAO")
        if leaf_call.wrapper_path:
            tags.append(f"封装路径: {' -> '.join(leaf_call.wrapper_path)}")

        lines = [
            f"<b>{title}</b>",
            f"状态: <b>{'成功' if success else '失败'}</b>",
            f"调用: <code>{leaf_call.pallet}.{leaf_call.call_name}</code>",
            f"区块: <code>{block_number}</code>",
            f"Extrinsic: <code>{extrinsic_index}</code>",
            f"签名者: <code>{signer_address or '-'}</code>",
            f"金额估值: <b>{amount_tao:.6f} TAO</b>",
            f"主路径: <code>{primary_from or '-'} -> {primary_to or '-'}</code>",
            f"关联地址: <code>{', '.join(involved_addresses[:8]) if involved_addresses else '-'}</code>",
        ]
        if tags:
            lines.append(f"命中原因: {', '.join(tags)}")
        if failure_reason:
            lines.append(f"失败原因: <code>{failure_reason}</code>")
        return "\n".join(lines)

    async def _persist_and_notify(self, actions: list[ActionRecord]) -> None:
        # 先按账号入库去重，再分别推送到各自的 Telegram。
        if not actions:
            return

        for action in actions:
            with session_scope() as session:
                exists = session.scalar(
                    select(ChainEvent).where(
                        ChainEvent.owner_user_id == action.owner_user_id,
                        ChainEvent.block_number == action.block_number,
                        ChainEvent.event_index == action.event_index,
                    )
                )
                if exists:
                    continue

                row = ChainEvent(
                    owner_user_id=action.owner_user_id,
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

            if not action.should_notify:
                continue

            sent = False
            try:
                sent = await self._notifier.send_message(
                    token=action.telegram_bot_token,
                    chat_id=action.telegram_chat_id,
                    text=action.message,
                )
            except Exception:
                logger.exception("telegram send failed for user %s", action.owner_user_id)

            if sent:
                with session_scope() as session:
                    stored = session.scalar(
                        select(ChainEvent).where(
                            ChainEvent.owner_user_id == action.owner_user_id,
                            ChainEvent.block_number == action.block_number,
                            ChainEvent.event_index == action.event_index,
                        )
                    )
                    if stored:
                        stored.notification_sent = True

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
        if isinstance(normalized, str):
            digits = normalized.replace(",", "").replace("_", "")
            if digits.isdigit():
                return int(digits)
        return None


def ensure_state(session) -> MonitorState:
    # 监听状态表只存一行，不存在时自动创建。
    state = session.get(MonitorState, 1)
    if state is None:
        state = MonitorState(id=1, monitor_status="idle", last_scanned_block=0, last_seen_head=0)
        session.add(state)
        session.flush()
    return state

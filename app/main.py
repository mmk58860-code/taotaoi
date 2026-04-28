from __future__ import annotations

import csv
import io
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from starlette.middleware.sessions import SessionMiddleware

from app.config import BASE_DIR, get_settings
from app.database import Base, engine, run_startup_migrations, session_scope
from app.models import AdminUser, ChainEvent, MonitorMenu, UserSetting, WalletWatch
from app.schemas import (
    MonitorMenuCreate,
    MonitorMenuRename,
    MonitorMenuSettingsUpdate,
    SystemSettingsUpdate,
    WalletCreate,
)
from app.services.auth import (
    authenticate_user,
    bootstrap_admin_user,
    decrypt_password_for_display,
    encrypt_password_for_display,
    hash_password,
)
from app.services.monitor_menu_service import (
    BUILTIN_ALERT_KIND,
    BUILTIN_WALLET_KIND,
    bootstrap_monitor_menus,
    create_custom_wallet_menu,
    get_builtin_menu,
    get_menu_runtime_settings,
    get_monitor_menu,
    list_monitor_menus,
    migrate_legacy_user_settings_to_menus,
    rename_monitor_menu,
    update_menu_runtime_settings,
)
from app.services.settings_service import (
    bootstrap_system_settings,
    get_system_runtime_settings,
    update_system_runtime_settings,
)
from app.services.subtensor_monitor import ACTION_TITLES, SubtensorMonitor, ensure_state
from app.services.telegram import TelegramNotifier


# 统一日志格式，方便部署后直接查看 systemd 日志。
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# 全局单例对象：监听器、TG 发送器、配置对象、模板引擎。
monitor = SubtensorMonitor()
notifier = TelegramNotifier()
app_settings = get_settings()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
IMPORT_EXPORT_FORMAT_VERSION = 1
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 先建表，再跑迁移，最后补齐默认数据，保证旧项目升级时尽量不丢资料。
    Base.metadata.create_all(bind=engine)
    with session_scope() as session:
        bootstrap_system_settings(session)
        bootstrap_admin_user(session)
        superadmin = session.scalar(
            select(AdminUser).where(AdminUser.is_superadmin.is_(True)).order_by(AdminUser.id.asc())
        )
    if superadmin:
        run_startup_migrations(superadmin.id)

    Base.metadata.create_all(bind=engine)
    with session_scope() as session:
        bootstrap_system_settings(session)
        bootstrap_admin_user(session)
        superadmin = session.scalar(
            select(AdminUser).where(AdminUser.is_superadmin.is_(True)).order_by(AdminUser.id.asc())
        )
        admin_users = session.scalars(select(AdminUser).order_by(AdminUser.id.asc())).all()
        if superadmin:
            for admin_user in admin_users:
                bootstrap_monitor_menus(session, admin_user.id)
                migrate_legacy_user_settings_to_menus(session, admin_user.id)
        ensure_state(session)

    await monitor.start()
    try:
        yield
    finally:
        await monitor.stop()


app = FastAPI(title="TAO Monitor", lifespan=lifespan)

# 使用会话中间件保存网页登录状态。
app.add_middleware(SessionMiddleware, secret_key=app_settings.secret_key, same_site="lax")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


def is_authenticated(request: Request) -> bool:
    # 登录成功后会在 session 里写入 authenticated 标记。
    return bool(request.session.get("authenticated"))


def is_superadmin(request: Request) -> bool:
    # 只有总管理员才允许管理系统链路设置和后台账号。
    return bool(request.session.get("is_superadmin"))


def current_user_id(request: Request) -> int:
    # 当前登录账号的主键会放进 session，供后续所有数据隔离逻辑使用。
    return int(request.session.get("user_id", 0))


def login_redirect() -> RedirectResponse:
    # 未登录时统一跳回登录页。
    return RedirectResponse("/login", status_code=303)


def redirect_with_notice(
    message: str,
    level: str = "success",
    target: str = "/",
    active_panel: str = "",
    extra_query: dict[str, str | int] | None = None,
) -> RedirectResponse:
    # 操作结果通过查询参数带回首页，页面顶部显示提示条。
    query_params = {"notice": message, "level": level}
    if active_panel:
        query_params["panel"] = active_panel
    if extra_query:
        query_params.update({key: str(value) for key, value in extra_query.items()})
    query = urlencode(query_params)
    separator = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{separator}{query}", status_code=303)


def require_superadmin(request: Request, message: str) -> RedirectResponse | None:
    # 普通账号不允许碰系统级设置和后台账号管理。
    if not is_superadmin(request):
        return redirect_with_notice(message, level="error")
    return None


def build_wallet_backup_csv(wallets: list[WalletWatch]) -> bytes:
    # 兼容旧逻辑保留的 CSV 导出函数，目前主要导出流程已改成 JSON 资料包。
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["备注别名", "钱包地址", "监控状态", "添加时间"])
    for wallet in wallets:
        writer.writerow(
            [
                wallet.alias,
                wallet.address,
                "启用" if wallet.enabled else "暂停",
                to_beijing_string(wallet.created_at),
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")


def import_staging_dir() -> Path:
    # 资料导入的冲突处理需要短暂保存预览数据，放在 data/import_staging 下。
    path = BASE_DIR / "data" / "import_staging"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_menu_data_export(menu: MonitorMenu, wallets: list[WalletWatch], username: str) -> bytes:
    # 导出当前监控菜单的钱包地址、备注和 TG 机器人信息，便于后续导入恢复。
    payload = {
        "format_version": IMPORT_EXPORT_FORMAT_VERSION,
        "exported_at": to_beijing_iso(datetime.utcnow()),
        "exported_by": username,
        "menu": {
            "name": menu.name,
            "menu_kind": menu.menu_kind,
            "telegram_bot_token": menu.telegram_bot_token,
            "telegram_chat_id": menu.telegram_chat_id,
        },
        "wallets": [
            {
                "address": wallet.address,
                "alias": wallet.alias,
                "enabled": wallet.enabled,
            }
            for wallet in wallets
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def to_beijing_time(dt: datetime | None) -> datetime:
    # 数据库存的是 UTC 风格时间，这里统一转成北京时间给页面和导出使用。
    if dt is None:
        return datetime.now(BEIJING_TZ)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(BEIJING_TZ)


def to_beijing_string(dt: datetime | None) -> str:
    # 页面展示统一格式化成北京时间字符串。
    return to_beijing_time(dt).strftime("%Y-%m-%d %H:%M:%S")


def to_beijing_iso(dt: datetime | None) -> str:
    # 给接口返回 ISO 字符串时也统一转北京时间，避免前后不一致。
    return to_beijing_time(dt).isoformat()


def action_label(event: ChainEvent) -> str:
    # 服务概览里优先显示中文动作名；没有映射时再回退到原始调用名。
    label = ACTION_TITLES.get(event.action_type, "")
    if not label:
        raw_name = event.call_name or event.event_name or event.action_type or "未知动作"
        label = f"{event.pallet}.{raw_name}"
    if not event.success:
        return f"{label}（失败）"
    return label


def event_trade_signal(event: ChainEvent) -> dict[str, object]:
    # 不改数据库结构，直接从原始链上参数里提取短线交易需要看的字段。
    normalized_amount = normalized_trade_amount_tao(event)
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
    direction = direction_map.get(event.action_type, "链上动作")
    try:
        raw = json.loads(event.raw_payload or "{}")
        params = raw.get("leaf_call", raw) if isinstance(raw, dict) else raw
        subnet_ids = extract_subnet_ids(params)
        subnet_label = subnet_label_for_action(event.action_type, subnet_ids)
    except Exception:
        subnet_label = subnet_label_for_action(event.action_type, [])

    if event.action_type in {"stake_add", "stake_remove", "stake_move", "stake_transfer", "stake_swap", "swap_call"}:
        if normalized_amount >= 100:
            signal = f"大额{direction}"
        elif normalized_amount >= 10:
            signal = f"中额{direction}"
        elif normalized_amount > 0:
            signal = f"小额{direction}"
        else:
            signal = direction
    elif event.action_type == "transfer" and normalized_amount >= 100:
        signal = "大额资金转移"
    else:
        signal = direction
    amount_label = f"{normalized_amount:.6f} TAO"
    if normalized_amount <= 0 and event.action_type in {
        "stake_remove",
        "stake_move",
        "stake_transfer",
        "stake_swap",
        "swap_call",
    }:
        amount_label = "未确认 TAO 成交额"

    return {
        "subnet": subnet_label,
        "direction": direction,
        "signal": signal,
        "amount_tao": normalized_amount,
        "amount_label": amount_label,
    }


def normalized_trade_amount_tao(event: ChainEvent) -> float:
    # 历史记录可能用旧规则把 Alpha/price 误算成 TAO，这里展示信号时重新按官方语义估值。
    try:
        raw = json.loads(event.raw_payload or "{}")
    except Exception:
        return float(event.amount_tao or 0)
    if not isinstance(raw, dict):
        return float(event.amount_tao or 0)
    params = raw.get("leaf_call", raw)
    related_events = raw.get("related_events", [])
    action_type = str(raw.get("action_type") or event.action_type or "")
    if action_type in {"weights_set", "weights_commit", "weights_reveal", "children_set", "identity_set"}:
        return 0.0

    candidates = collect_settlement_tao_from_events(action_type, related_events)
    if action_type == "transfer":
        candidates.extend(collect_amount_candidates(params, include_generic_amount=True))
    elif action_type == "stake_add":
        candidates.extend(
            collect_amount_candidates(
                params,
                include_generic_amount=True,
                include_stake_amount=True,
            )
        )
    elif action_type in {"stake_remove", "stake_move", "stake_transfer", "stake_swap", "swap_call"}:
        if isinstance(related_events, list):
            for related_event in related_events:
                candidates.extend(collect_tao_amount_candidates(related_event))
    else:
        candidates.extend(collect_tao_amount_candidates(params))
    if not candidates:
        return 0.0
    return round(max(candidates) / 1_000_000_000, 9)


def subnet_label_for_action(action_type: str, subnet_ids: list[int]) -> str:
    # 余额普通转账本身不带子网字段，显示“无子网字段”比“未知”更准确。
    if subnet_ids:
        return "、".join(f"子网 {netuid}" for netuid in subnet_ids[:3])
    if action_type == "transfer":
        return "无子网字段"
    return "未知子网"


def extract_subnet_ids(payload) -> list[int]:
    # 常见字段包括 netuid、subnet、network，递归提取后去重。
    results: list[int] = []
    subnet_keys = ("netuid", "subnet", "network", "destination_netuid", "origin_netuid")

    if isinstance(payload, dict):
        param_name = str(payload.get("name", payload.get("param", ""))).lower()
        if any(token in param_name for token in subnet_keys):
            parsed = to_int(payload.get("value"))
            if parsed is not None and 0 <= parsed <= 10_000:
                results.append(parsed)
        for key, value in payload.items():
            key_text = str(key).lower()
            if any(token in key_text for token in subnet_keys):
                parsed = to_int(value)
                if parsed is not None and 0 <= parsed <= 10_000:
                    results.append(parsed)
            results.extend(extract_subnet_ids(value))
    elif isinstance(payload, list):
        for item in payload:
            results.extend(extract_subnet_ids(item))

    return list(dict.fromkeys(results))


def collect_tao_amount_candidates(payload) -> list[int]:
    # 只吃明确带 TAO/rao/手续费/销毁成本的字段；不把普通 amount 当 TAO。
    return collect_amount_candidates(payload)


def collect_settlement_tao_from_events(action_type: str, related_events) -> list[int]:
    # Subtensor 质押事件的 TAO 结算字段常常按固定位置出现，不一定有 tao 字段名。
    if not isinstance(related_events, list):
        return []
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
    results: list[int] = []
    for event in related_events:
        event_name = event_name_from_payload(event).lower()
        if expected_events and event_name not in expected_events:
            continue
        values = event_attribute_values(event)
        tao_index = event_tao_index.get(event_name)
        if tao_index is not None and len(values) > tao_index:
            parsed = to_int(values[tao_index])
            if parsed is not None and parsed > 0:
                results.append(parsed)
    return results


def event_name_from_payload(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("event_id", "event", "name"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    nested = payload.get("event")
    if isinstance(nested, dict):
        return event_name_from_payload(nested)
    return ""


def event_attribute_values(payload) -> list:
    if isinstance(payload, dict):
        for key in ("attributes", "params", "args", "data", "values"):
            value = payload.get(key)
            if isinstance(value, list):
                return event_attribute_values(value)
        if all(str(key).isdigit() for key in payload):
            return [payload[key] for key in sorted(payload, key=lambda item: int(str(item)))]
    if isinstance(payload, list):
        values = []
        for item in payload:
            if isinstance(item, dict) and "value" in item:
                values.append(item.get("value"))
            else:
                values.append(item)
        return values
    return []


def collect_amount_candidates(
    payload,
    *,
    include_generic_amount: bool = False,
    include_stake_amount: bool = False,
) -> list[int]:
    # 页面展示用的保守 TAO 金额提取，避免把 Alpha/price/netuid/value 误当成 TAO。
    results: list[int] = []
    tao_keys = ("tao", "rao", "burn", "fee", "cost")
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

    if isinstance(payload, dict):
        param_name = str(payload.get("name", payload.get("param", ""))).lower()
        if is_amount_key(param_name):
            parsed = to_int(payload.get("value"))
            if parsed is not None and parsed > 0:
                results.append(parsed)
        for key, value in payload.items():
            key_text = str(key).lower()
            if key_text != "value" and is_amount_key(key_text):
                parsed = to_int(value)
                if parsed is not None and parsed > 0:
                    results.append(parsed)
            results.extend(
                collect_amount_candidates(
                    value,
                    include_generic_amount=include_generic_amount,
                    include_stake_amount=include_stake_amount,
                )
            )
    elif isinstance(payload, list):
        contains_address = any(isinstance(item, str) and len(item) >= 40 for item in payload)
        if include_generic_amount and contains_address:
            for item in payload:
                parsed = to_int(item)
                if parsed is not None and parsed > 0:
                    results.append(parsed)
        for item in payload:
            results.extend(
                collect_amount_candidates(
                    item,
                    include_generic_amount=include_generic_amount,
                    include_stake_amount=include_stake_amount,
                )
            )
    return results


def to_int(value) -> int | None:
    # 链上参数里数字可能是 int、十六进制字符串或普通字符串。
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16) if value.startswith("0x") else int(value)
        except ValueError:
            return None
    if isinstance(value, dict) and "value" in value:
        return to_int(value.get("value"))
    return None


# 把页面常用函数注册成模板全局函数，避免某个渲染入口漏传后导致后台 500。
templates.env.globals["event_trade_signal"] = event_trade_signal


def parse_menu_data_import(file_bytes: bytes) -> dict[str, object]:
    # 资料导入只接受项目自己导出的 JSON，避免字段不一致导致误导入。
    try:
        payload = json.loads(file_bytes.decode("utf-8-sig"))
    except Exception as exc:
        raise ValueError("导入文件格式无法识别，请上传系统导出的 JSON 文件") from exc

    if not isinstance(payload, dict):
        raise ValueError("导入文件内容无效")
    if payload.get("format_version") != IMPORT_EXPORT_FORMAT_VERSION:
        raise ValueError("导入文件版本不匹配，请使用当前系统导出的资料文件")

    menu = payload.get("menu", {})
    wallets = payload.get("wallets", [])
    if not isinstance(menu, dict) or not isinstance(wallets, list):
        raise ValueError("导入文件缺少必要字段")

    normalized_wallets: list[dict[str, object]] = []
    seen_addresses: set[str] = set()
    for row in wallets:
        if not isinstance(row, dict):
            continue
        address = str(row.get("address", "")).strip()
        alias = str(row.get("alias", "")).strip()
        if not address or not alias or address in seen_addresses:
            continue
        normalized_wallets.append(
            {
                "address": address,
                "alias": alias,
                "enabled": bool(row.get("enabled", True)),
            }
        )
        seen_addresses.add(address)

    return {
        "menu_name": str(menu.get("name", "")).strip(),
        "menu_kind": str(menu.get("menu_kind", BUILTIN_WALLET_KIND)).strip() or BUILTIN_WALLET_KIND,
        "telegram_bot_token": str(menu.get("telegram_bot_token", "")).strip(),
        "telegram_chat_id": str(menu.get("telegram_chat_id", "")).strip(),
        "wallets": normalized_wallets,
    }


def build_import_preview(existing_wallets: list[WalletWatch], imported_wallets: list[dict[str, object]]) -> dict[str, object]:
    # 导入预检分成三类：完全重复、可直接新增、同地址不同备注冲突。
    existing_by_address = {wallet.address: wallet for wallet in existing_wallets}
    duplicates = 0
    additions: list[dict[str, object]] = []
    conflicts: list[dict[str, str]] = []

    for row in imported_wallets:
        address = str(row["address"])
        imported_alias = str(row["alias"])
        existing = existing_by_address.get(address)
        if existing is None:
            additions.append(row)
            continue
        if existing.alias == imported_alias:
            duplicates += 1
            continue
        conflicts.append(
            {
                "address": address,
                "existing_alias": existing.alias,
                "imported_alias": imported_alias,
            }
        )

    return {
        "duplicate_count": duplicates,
        "additions": additions,
        "conflicts": conflicts,
    }


def save_import_preview(owner_user_id: int, menu_id: int, payload: dict[str, object]) -> str:
    # 冲突预览临时保存到文件，避免把大量数据塞进浏览器 session cookie。
    token = uuid4().hex
    target = import_staging_dir() / f"{token}.json"
    target.write_text(
        json.dumps(
            {
                "owner_user_id": owner_user_id,
                "menu_id": menu_id,
                "payload": payload,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return token


def load_import_preview(token: str) -> dict[str, object] | None:
    # 读取临时导入预览文件，供页面显示冲突选择。
    if not token:
        return None
    target = import_staging_dir() / f"{token}.json"
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None


def delete_import_preview(token: str) -> None:
    # 导入确认完成后清理临时文件，避免堆积。
    if not token:
        return
    target = import_staging_dir() / f"{token}.json"
    if target.exists():
        target.unlink(missing_ok=True)


def apply_import_preview(
    session,
    owner_user_id: int,
    menu: MonitorMenu,
    payload: dict[str, object],
    resolutions: dict[str, str] | None = None,
) -> dict[str, int]:
    # 把预览结果真正写入数据库：新增钱包、按选择解决备注冲突、同步 TG 信息。
    preview = payload.get("preview", {})
    imported = payload.get("imported", {})
    additions = preview.get("additions", []) if isinstance(preview, dict) else []
    conflicts = preview.get("conflicts", []) if isinstance(preview, dict) else []

    created = 0
    updated_aliases = 0

    for row in additions:
        if not isinstance(row, dict):
            continue
        session.add(
            WalletWatch(
                owner_user_id=owner_user_id,
                monitor_menu_id=menu.id,
                address=str(row["address"]),
                alias=str(row["alias"]),
                enabled=bool(row.get("enabled", True)),
            )
        )
        created += 1

    resolution_map = resolutions or {}
    for index, conflict in enumerate(conflicts):
        if not isinstance(conflict, dict):
            continue
        address = str(conflict["address"])
        choice = resolution_map.get(str(index), "keep_existing")
        if choice != "use_imported":
            continue
        wallet = session.scalar(
            select(WalletWatch).where(
                WalletWatch.owner_user_id == owner_user_id,
                WalletWatch.monitor_menu_id == menu.id,
                WalletWatch.address == address,
            )
        )
        if wallet is None:
            continue
        wallet.alias = str(conflict["imported_alias"])
        updated_aliases += 1

    if isinstance(imported, dict):
        menu.telegram_bot_token = str(imported.get("telegram_bot_token", ""))
        menu.telegram_chat_id = str(imported.get("telegram_chat_id", ""))
    session.flush()
    return {"created": created, "updated_aliases": updated_aliases}


def wallet_query_for_menu(user_id: int, menu_id: int):
    # 钱包监控按菜单分组，同一个账号下不同菜单之间互相隔离。
    return select(WalletWatch).where(
        WalletWatch.owner_user_id == user_id,
        WalletWatch.monitor_menu_id == menu_id,
    )


def event_query_for_user(user_id: int):
    # 事件记录也按账号隔离，避免朋友之间互相看到彼此的监控结果。
    return select(ChainEvent).where(ChainEvent.owner_user_id == user_id)


def get_owned_menu(session, request: Request, menu_id: int) -> MonitorMenu | None:
    # 所有菜单操作都必须校验归属，普通账号绝不能操作别人菜单。
    row = session.get(MonitorMenu, menu_id)
    if row is None:
        return None
    if row.owner_user_id != current_user_id(request):
        return None
    return row


def get_owned_wallet(session, request: Request, menu_id: int, wallet_id: int) -> WalletWatch | None:
    # 所有钱包操作都必须校验归属，普通账号绝不能操作别人数据。
    row = session.get(WalletWatch, wallet_id)
    if row is None:
        return None
    if row.owner_user_id != current_user_id(request):
        return None
    if row.monitor_menu_id != menu_id:
        return None
    return row


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # 已登录时直接跳首页，避免重复显示登录页。
    if is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": request.query_params.get("error", ""),
        },
    )


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    # 登录成功后把用户主键、用户名和权限写进 session。
    with session_scope() as session:
        user = authenticate_user(session, username=username, password=password)
        if user:
            bootstrap_monitor_menus(session, user.id)
    if user is None:
        return RedirectResponse(
            "/login?error=%E7%94%A8%E6%88%B7%E5%90%8D%E6%88%96%E5%AF%86%E7%A0%81%E4%B8%8D%E6%AD%A3%E7%A1%AE",
            status_code=303,
        )
    request.session["authenticated"] = True
    request.session["user_id"] = user.id
    request.session["username"] = user.username
    request.session["is_superadmin"] = user.is_superadmin
    return redirect_with_notice("登录成功")


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    # 退出登录时直接清空当前 session。
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    # 首页负责展示当前账号的监控菜单、事件、通知设置，以及总管理员专属功能。
    if not is_authenticated(request):
        return login_redirect()

    user_id = current_user_id(request)
    import_token = request.query_params.get("import_token", "")
    active_import_preview = load_import_preview(import_token)
    import_result = None
    if request.query_params.get("import_created") or request.query_params.get("import_updated") or request.query_params.get("import_skipped"):
        import_result = {
            "created": int(request.query_params.get("import_created", "0") or 0),
            "updated": int(request.query_params.get("import_updated", "0") or 0),
            "skipped": int(request.query_params.get("import_skipped", "0") or 0),
            "panel": request.query_params.get("panel", ""),
        }
    with session_scope() as session:
        monitor_menus = list_monitor_menus(session, user_id)
        wallet_menus = [menu for menu in monitor_menus if menu.menu_kind == BUILTIN_WALLET_KIND]
        alert_menu = get_builtin_menu(session, user_id, BUILTIN_ALERT_KIND)
        menu_settings_map = {
            menu.id: get_menu_runtime_settings(session, user_id, menu.id)
            for menu in monitor_menus
        }
        wallets = session.scalars(
            select(WalletWatch)
            .where(WalletWatch.owner_user_id == user_id)
            .order_by(WalletWatch.created_at.desc())
        ).all()
        wallets_by_menu = {
            menu.id: session.scalars(
                wallet_query_for_menu(user_id, menu.id).order_by(WalletWatch.created_at.desc())
            ).all()
            for menu in wallet_menus
        }
        events = session.scalars(
            event_query_for_user(user_id).order_by(ChainEvent.detected_at.desc()).limit(50)
        ).all()
        admin_users = session.scalars(select(AdminUser).order_by(AdminUser.created_at.asc())).all() if is_superadmin(request) else []
        state = ensure_state(session)
        system_settings = get_system_runtime_settings(session) if is_superadmin(request) else {}
        total_events = session.scalar(
            select(func.count()).select_from(ChainEvent).where(ChainEvent.owner_user_id == user_id)
        ) or 0

    requested_panel = request.query_params.get("panel", "")
    valid_panels = {"overview-panel"}
    if is_superadmin(request):
        valid_panels.update({"system-panel", "accounts-panel"})
    valid_panels.update(f"monitor-menu-{menu.id}" for menu in monitor_menus)
    active_panel = requested_panel if requested_panel in valid_panels else "overview-panel"

    if active_import_preview:
        if int(active_import_preview.get("owner_user_id", 0)) != user_id:
            active_import_preview = None

    admin_user_passwords = (
        {
            row.id: (decrypt_password_for_display(row.password_ciphertext) or "历史账号未保存可回显密码")
            for row in admin_users
        }
        if is_superadmin(request)
        else {}
    )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "wallets": wallets,
            "wallets_by_menu": wallets_by_menu,
            "events": events,
            "admin_users": admin_users,
            "admin_user_passwords": admin_user_passwords,
            "monitor_menus": monitor_menus,
            "wallet_menus": wallet_menus,
            "alert_menu": alert_menu,
            "menu_settings_map": menu_settings_map,
            "active_import_preview": active_import_preview,
            "active_import_token": import_token,
            "import_result": import_result,
            "active_panel": active_panel,
            "state": state,
            "system_settings": system_settings,
            "total_events": total_events,
            "active_wallets": sum(1 for wallet in wallets if wallet.enabled),
            "notice": request.query_params.get("notice", ""),
            "level": request.query_params.get("level", "success"),
            "current_username": request.session.get("username", ""),
            "current_is_superadmin": is_superadmin(request),
            "to_beijing_string": to_beijing_string,
            "action_label": action_label,
            "event_trade_signal": event_trade_signal,
        },
    )


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    # 健康检查接口，方便反向代理和监控系统探活。
    return {"status": "ok"}


@app.get("/api/state")
async def api_state(request: Request) -> JSONResponse:
    # 给前端轮询使用的轻量状态接口，只返回当前账号自己的统计信息。
    if not is_authenticated(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    user_id = current_user_id(request)
    with session_scope() as session:
        state = ensure_state(session)
        wallets = session.scalar(
            select(func.count()).select_from(WalletWatch).where(WalletWatch.owner_user_id == user_id)
        ) or 0
        active_wallets = session.scalar(
            select(func.count())
            .select_from(WalletWatch)
            .where(WalletWatch.owner_user_id == user_id, WalletWatch.enabled.is_(True))
        ) or 0
        events = session.scalar(
            select(func.count()).select_from(ChainEvent).where(ChainEvent.owner_user_id == user_id)
        ) or 0
        latest = session.scalars(
            event_query_for_user(user_id).order_by(desc(ChainEvent.detected_at)).limit(10)
        ).all()
        alert_menu = get_builtin_menu(session, user_id, BUILTIN_ALERT_KIND)
        alert_settings = get_menu_runtime_settings(session, user_id, alert_menu.id) if alert_menu else {}

    return JSONResponse(
        {
            "monitor_status": state.monitor_status,
            "last_scanned_block": state.last_scanned_block,
            "last_seen_head": state.last_seen_head,
            "last_error": state.last_error,
            "wallet_count": wallets,
            "active_wallet_count": active_wallets,
            "event_count": events,
            "threshold_tao": alert_settings.get("large_transfer_threshold_tao"),
            "server_online": True,
            "events": [
                {
                    "id": row.id,
                    "block_number": row.block_number,
                    "action": action_label(row),
                    "amount_tao": event_trade_signal(row)["amount_tao"],
                    "trade_signal": event_trade_signal(row),
                    "message": row.message,
                    "detected_at": to_beijing_iso(row.detected_at),
                }
                for row in latest
            ],
        }
    )


@app.post("/monitor-menus")
async def create_monitor_menu(request: Request, name: str = Form(...)) -> JSONResponse:
    # 左侧“+”号用于新增自定义钱包监控菜单。
    if not is_authenticated(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    user_id = current_user_id(request)
    payload = MonitorMenuCreate(name=name.strip())
    with session_scope() as session:
        row = create_custom_wallet_menu(session, user_id, payload)
    return JSONResponse({"ok": True, "menu_id": row.id, "name": row.name})


@app.post("/monitor-menus/{menu_id}/rename")
async def rename_monitor_menu_route(request: Request, menu_id: int, name: str = Form(...)) -> JSONResponse:
    # 菜单支持双击改名，基础菜单和自定义菜单都可以改名。
    if not is_authenticated(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    user_id = current_user_id(request)
    payload = MonitorMenuRename(name=name.strip())
    with session_scope() as session:
        row = rename_monitor_menu(session, user_id, menu_id, payload)
        if row is None:
            return JSONResponse({"detail": "menu_not_found"}, status_code=404)
    return JSONResponse({"ok": True, "menu_id": menu_id, "name": row.name})


@app.post("/monitor-menus/{menu_id}/delete")
async def delete_monitor_menu(request: Request, menu_id: int) -> RedirectResponse:
    # 只允许删除自定义的钱包监控菜单，内置菜单受保护。
    if not is_authenticated(request):
        return login_redirect()

    with session_scope() as session:
        menu = get_owned_menu(session, request, menu_id)
        if menu is None:
            return redirect_with_notice("监控菜单不存在", level="error", active_panel="overview-panel")
        if menu.is_builtin:
            return redirect_with_notice("基础监控菜单不能删除", level="error", active_panel=f"monitor-menu-{menu_id}")

        wallet_rows = session.scalars(select(WalletWatch).where(WalletWatch.monitor_menu_id == menu_id)).all()
        event_rows = session.scalars(select(ChainEvent).where(ChainEvent.monitor_menu_id == menu_id)).all()
        menu_name = menu.name

        for row in wallet_rows:
            session.delete(row)
        for row in event_rows:
            session.delete(row)
        session.delete(menu)

    await monitor.restart()
    return redirect_with_notice(f"监控菜单 {menu_name} 已删除", active_panel="overview-panel")


@app.post("/monitor-menus/{menu_id}/wallets")
async def create_wallet(
    request: Request,
    menu_id: int,
    address: str = Form(...),
    alias: str = Form(...),
    next_panel: str = Form(""),
) -> RedirectResponse:
    # 新增钱包时写入当前登录账号、当前监控菜单自己的空间。
    if not is_authenticated(request):
        return login_redirect()

    payload = WalletCreate(address=address.strip(), alias=alias.strip())
    user_id = current_user_id(request)
    with session_scope() as session:
        menu = get_owned_menu(session, request, menu_id)
        if menu is None or menu.menu_kind != BUILTIN_WALLET_KIND:
            return redirect_with_notice("监控菜单不存在或不支持钱包列表", level="error", active_panel=next_panel)
        existing = session.scalar(
            select(WalletWatch).where(
                WalletWatch.owner_user_id == user_id,
                WalletWatch.monitor_menu_id == menu_id,
                WalletWatch.address == payload.address,
            )
        )
        if existing:
            return redirect_with_notice("这个钱包地址已经存在于当前监控菜单", level="error", active_panel=next_panel)
        session.add(
            WalletWatch(
                owner_user_id=user_id,
                monitor_menu_id=menu_id,
                address=payload.address,
                alias=payload.alias,
                enabled=True,
            )
        )
    await monitor.restart()
    return redirect_with_notice("钱包已添加", active_panel=next_panel or f"monitor-menu-{menu_id}")


@app.post("/monitor-menus/{menu_id}/wallets/{wallet_id}/toggle")
async def toggle_wallet(request: Request, menu_id: int, wallet_id: int, next_panel: str = Form("")) -> RedirectResponse:
    # 钱包支持临时暂停，不需要删除，也只影响当前账号自己的监控。
    if not is_authenticated(request):
        return login_redirect()

    with session_scope() as session:
        row = get_owned_wallet(session, request, menu_id, wallet_id)
        if row is None:
            return redirect_with_notice("钱包不存在或不属于当前监控菜单", level="error", active_panel=next_panel)
        row.enabled = not row.enabled
        label = f"{row.alias} 已{'启用' if row.enabled else '暂停'}监控"

    await monitor.restart()
    return redirect_with_notice(label, active_panel=next_panel or f"monitor-menu-{menu_id}")


@app.post("/monitor-menus/{menu_id}/wallets/{wallet_id}/delete")
async def delete_wallet(request: Request, menu_id: int, wallet_id: int, next_panel: str = Form("")) -> RedirectResponse:
    # 删除钱包后，监听器也会同步重载配置。
    if not is_authenticated(request):
        return login_redirect()

    with session_scope() as session:
        row = get_owned_wallet(session, request, menu_id, wallet_id)
        if row is None:
            return redirect_with_notice("钱包不存在或不属于当前监控菜单", level="error", active_panel=next_panel)
        label = f"{row.alias} 已删除"
        session.delete(row)

    await monitor.restart()
    return redirect_with_notice(label, active_panel=next_panel or f"monitor-menu-{menu_id}")


@app.post("/settings/system")
async def save_system_settings(request: Request, next_panel: str = Form("")) -> RedirectResponse:
    # 只有总管理员可以修改链节点、扫描间隔这类系统级参数。
    if not is_authenticated(request):
        return login_redirect()
    forbidden = require_superadmin(request, "只有总管理员可以修改系统设置")
    if forbidden is not None:
        return forbidden

    form = await request.form()
    payload = SystemSettingsUpdate(
        subtensor_ws_url=str(form.get("subtensor_ws_url", "")).strip(),
        network_name=str(form.get("network_name", "")).strip(),
        poll_interval_seconds=int(form.get("poll_interval_seconds", 2)),
        finality_lag_blocks=int(form.get("finality_lag_blocks", 0)),
    )

    with session_scope() as session:
        update_system_runtime_settings(session, payload)

    await monitor.restart()
    return redirect_with_notice("系统设置已保存", active_panel=next_panel or "system-panel")


@app.post("/monitor-menus/{menu_id}/settings")
async def save_monitor_menu_settings(request: Request, menu_id: int, next_panel: str = Form("")) -> RedirectResponse:
    # 每个监控菜单都可以保存自己的 TG 参数；大额预警菜单还可以维护自己的阈值。
    if not is_authenticated(request):
        return login_redirect()

    form = await request.form()
    payload = MonitorMenuSettingsUpdate(
        large_transfer_threshold_tao=float(form.get("large_transfer_threshold_tao", 0)),
        telegram_bot_token=str(form.get("telegram_bot_token", "")).strip(),
        telegram_chat_id=str(form.get("telegram_chat_id", "")).strip(),
    )

    with session_scope() as session:
        menu = get_owned_menu(session, request, menu_id)
        if menu is None:
            return redirect_with_notice("监控菜单不存在", level="error", active_panel=next_panel)
        update_menu_runtime_settings(session, current_user_id(request), menu_id, payload)

    await monitor.restart()
    return redirect_with_notice("当前监控菜单设置已保存", active_panel=next_panel or f"monitor-menu-{menu_id}")


@app.post("/monitor-menus/{menu_id}/test-telegram")
async def test_telegram(request: Request, menu_id: int, next_panel: str = Form("")) -> RedirectResponse:
    # 每个监控菜单都可以单独发送测试消息，验证自己绑定的机器人参数。
    if not is_authenticated(request):
        return login_redirect()

    with session_scope() as session:
        runtime = get_menu_runtime_settings(session, current_user_id(request), menu_id)
        if not runtime:
            return redirect_with_notice("监控菜单不存在", level="error", active_panel=next_panel)

    token = str(runtime.get("telegram_bot_token", ""))
    chat_id = str(runtime.get("telegram_chat_id", ""))
    if not token or not chat_id:
        return redirect_with_notice("请先保存当前监控菜单的 Telegram Bot Token 和 Chat ID", level="error", active_panel=next_panel)

    try:
        sent = await notifier.send_message(
            token=token,
            chat_id=chat_id,
            text="<b>TAO Monitor</b>\n当前监控菜单的 Telegram 测试消息发送成功。",
        )
    except Exception:
        return redirect_with_notice("Telegram 测试消息发送失败，请检查机器人参数", level="error", active_panel=next_panel)

    if not sent:
        return redirect_with_notice("Telegram 测试消息发送失败，请检查机器人参数", level="error", active_panel=next_panel)
    return redirect_with_notice("Telegram 测试消息已发送", active_panel=next_panel or f"monitor-menu-{menu_id}")


@app.post("/admin-users")
async def create_admin_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_panel: str = Form(""),
) -> RedirectResponse:
    # 总管理员可以创建朋友账号，并保留可回显密码供自己查看。
    if not is_authenticated(request):
        return login_redirect()
    forbidden = require_superadmin(request, "只有总管理员可以添加账号")
    if forbidden is not None:
        return forbidden

    normalized_username = username.strip()
    normalized_password = password.strip()
    if len(normalized_username) < 3:
        return redirect_with_notice("用户名至少需要 3 个字符", level="error", active_panel=next_panel)
    if len(normalized_password) < 6:
        return redirect_with_notice("密码至少需要 6 个字符", level="error", active_panel=next_panel)

    with session_scope() as session:
        existing = session.scalar(select(AdminUser).where(AdminUser.username == normalized_username))
        if existing:
            return redirect_with_notice("该用户名已经存在", level="error", active_panel=next_panel)
        new_user = AdminUser(
            username=normalized_username,
            password_hash=hash_password(normalized_password),
            password_ciphertext=encrypt_password_for_display(normalized_password),
            is_superadmin=False,
        )
        session.add(new_user)
        session.flush()
        bootstrap_monitor_menus(session, new_user.id)

    return redirect_with_notice(f"账号 {normalized_username} 已创建", active_panel=next_panel or "accounts-panel")


@app.post("/admin-users/{user_id}/delete")
async def delete_admin_user(request: Request, user_id: int, next_panel: str = Form("")) -> RedirectResponse:
    # 删除普通账号时，同时清掉该账号自己的钱包、事件和通知配置。
    if not is_authenticated(request):
        return login_redirect()
    forbidden = require_superadmin(request, "只有总管理员可以删除账号")
    if forbidden is not None:
        return forbidden

    with session_scope() as session:
        user = session.get(AdminUser, user_id)
        if user is None:
            return redirect_with_notice("账号不存在", level="error", active_panel=next_panel)
        if user.is_superadmin:
            return redirect_with_notice("总管理员账号不能在这里删除", level="error", active_panel=next_panel)

        menu_rows = session.scalars(select(MonitorMenu).where(MonitorMenu.owner_user_id == user.id)).all()
        wallet_rows = session.scalars(select(WalletWatch).where(WalletWatch.owner_user_id == user.id)).all()
        event_rows = session.scalars(select(ChainEvent).where(ChainEvent.owner_user_id == user.id)).all()
        settings_row = session.get(UserSetting, user.id)

        for row in menu_rows:
            session.delete(row)
        for row in wallet_rows:
            session.delete(row)
        for row in event_rows:
            session.delete(row)
        if settings_row:
            session.delete(settings_row)

        label = f"账号 {user.username} 已删除"
        session.delete(user)

    await monitor.restart()
    return redirect_with_notice(label, active_panel=next_panel or "accounts-panel")


@app.get("/monitor-menus/{menu_id}/data-transfer/export")
async def export_wallet_backup(request: Request, menu_id: int):
    # 资料导出只导出当前菜单自己的钱包地址、备注和 TG 机器人信息。
    if not is_authenticated(request):
        return login_redirect()

    user_id = current_user_id(request)
    with session_scope() as session:
        menu = get_owned_menu(session, request, menu_id)
        if menu is None or menu.menu_kind != BUILTIN_WALLET_KIND:
            return redirect_with_notice("监控菜单不存在或不支持资料导出", level="error", active_panel=f"monitor-menu-{menu_id}")
        wallets = session.scalars(wallet_query_for_menu(user_id, menu_id).order_by(WalletWatch.created_at.asc())).all()

    safe_name = str(menu.name).replace(" ", "_")
    filename = f"tao-menu-data-{request.session.get('username', 'user')}-{safe_name}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    payload = build_menu_data_export(menu, wallets, username=str(request.session.get("username", "user")))
    return Response(
        content=payload,
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/monitor-menus/{menu_id}/data-transfer/import-preview")
async def preview_menu_import(
    request: Request,
    menu_id: int,
    import_file: UploadFile = File(...),
    next_panel: str = Form(""),
) -> RedirectResponse:
    # 导入前先做预检：完全重复的自动过滤，备注冲突的先进入确认步骤。
    if not is_authenticated(request):
        return login_redirect()

    file_bytes = await import_file.read()
    try:
        imported = parse_menu_data_import(file_bytes)
    except ValueError as exc:
        return redirect_with_notice(str(exc), level="error", active_panel=next_panel or f"monitor-menu-{menu_id}")
    if imported.get("menu_kind") != BUILTIN_WALLET_KIND:
        return redirect_with_notice("导入文件不是钱包监控资料包，请重新选择正确文件", level="error", active_panel=next_panel or f"monitor-menu-{menu_id}")

    with session_scope() as session:
        menu = get_owned_menu(session, request, menu_id)
        if menu is None or menu.menu_kind != BUILTIN_WALLET_KIND:
            return redirect_with_notice("监控菜单不存在或不支持资料导入", level="error", active_panel=next_panel or f"monitor-menu-{menu_id}")
        existing_wallets = session.scalars(wallet_query_for_menu(current_user_id(request), menu_id)).all()
        preview = build_import_preview(existing_wallets, imported_wallets=list(imported["wallets"]))

        if not preview["conflicts"]:
            result = apply_import_preview(
                session,
                owner_user_id=current_user_id(request),
                menu=menu,
                payload={"imported": imported, "preview": preview},
            )
            message = f"资料导入完成：新增 {result['created']} 条，跳过重复 {preview['duplicate_count']} 条"
            return redirect_with_notice(
                message,
                active_panel=next_panel or f"monitor-menu-{menu_id}",
                extra_query={
                    "import_created": result["created"],
                    "import_updated": 0,
                    "import_skipped": preview["duplicate_count"],
                },
            )

    token = save_import_preview(
        owner_user_id=current_user_id(request),
        menu_id=menu_id,
        payload={"imported": imported, "preview": preview},
    )
    return redirect_with_notice(
        f"发现 {len(preview['conflicts'])} 个备注冲突，请先确认后再导入",
        level="error",
        active_panel=next_panel or f"monitor-menu-{menu_id}",
        target=f"/?import_token={token}",
    )


@app.post("/monitor-menus/{menu_id}/data-transfer/import-apply")
async def apply_menu_import(
    request: Request,
    menu_id: int,
    import_token: str = Form(""),
    next_panel: str = Form(""),
) -> RedirectResponse:
    # 冲突确认后正式导入，按用户选择更新备注。
    if not is_authenticated(request):
        return login_redirect()

    staged = load_import_preview(import_token)
    if not staged:
        return redirect_with_notice("导入预览已失效，请重新上传资料文件", level="error", active_panel=next_panel or f"monitor-menu-{menu_id}")
    if int(staged.get("owner_user_id", 0)) != current_user_id(request) or int(staged.get("menu_id", 0)) != menu_id:
        return redirect_with_notice("导入预览不属于当前账号或菜单", level="error", active_panel=next_panel or f"monitor-menu-{menu_id}")

    resolution_map: dict[str, str] = {}
    form = await request.form()
    for key, value in form.items():
        if not key.startswith("conflict_choice_"):
            continue
        resolution_map[key.removeprefix("conflict_choice_")] = str(value)

    with session_scope() as session:
        menu = get_owned_menu(session, request, menu_id)
        if menu is None or menu.menu_kind != BUILTIN_WALLET_KIND:
            return redirect_with_notice("监控菜单不存在或不支持资料导入", level="error", active_panel=next_panel or f"monitor-menu-{menu_id}")
        result = apply_import_preview(
            session,
            owner_user_id=current_user_id(request),
            menu=menu,
            payload=staged["payload"],
            resolutions=resolution_map,
        )

    delete_import_preview(import_token)
    await monitor.restart()
    preview_data = staged["payload"]["preview"]
    duplicate_count = int(preview_data.get("duplicate_count", 0)) if isinstance(preview_data, dict) else 0
    message = f"资料导入完成：新增 {result['created']} 条，更新备注 {result['updated_aliases']} 条，跳过重复 {duplicate_count} 条"
    return redirect_with_notice(
        message,
        active_panel=next_panel or f"monitor-menu-{menu_id}",
        extra_query={
            "import_created": result["created"],
            "import_updated": result["updated_aliases"],
            "import_skipped": duplicate_count,
        },
    )


@app.post("/monitor/restart")
async def restart_monitor(request: Request) -> RedirectResponse:
    # 监听器重载属于系统级动作，只开放给总管理员。
    if not is_authenticated(request):
        return login_redirect()
    forbidden = require_superadmin(request, "只有总管理员可以重载监听器")
    if forbidden is not None:
        return forbidden

    await monitor.restart()
    return redirect_with_notice("监听器已重新载入")

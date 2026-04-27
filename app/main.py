from __future__ import annotations

import csv
import io
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from starlette.middleware.sessions import SessionMiddleware

from app.config import BASE_DIR, get_settings
from app.database import Base, engine, run_startup_migrations, session_scope
from app.models import AdminUser, ChainEvent, UserSetting, WalletWatch
from app.schemas import SystemSettingsUpdate, UserNotificationSettingsUpdate, WalletCreate
from app.services.auth import (
    authenticate_user,
    bootstrap_admin_user,
    decrypt_password_for_display,
    encrypt_password_for_display,
    hash_password,
)
from app.services.settings_service import (
    bootstrap_system_settings,
    bootstrap_user_settings,
    get_system_runtime_settings,
    get_user_runtime_settings,
    migrate_legacy_user_settings,
    update_system_runtime_settings,
    update_user_runtime_settings,
)
from app.services.subtensor_monitor import SubtensorMonitor, ensure_state
from app.services.telegram import TelegramNotifier


# 统一日志格式，方便部署后直接查看 systemd 日志。
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# 全局单例对象：监听器、TG 发送器、配置对象、模板引擎。
monitor = SubtensorMonitor()
notifier = TelegramNotifier()
app_settings = get_settings()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


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
                bootstrap_user_settings(session, admin_user.id)
            migrate_legacy_user_settings(session, superadmin.id)
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


def redirect_with_notice(message: str, level: str = "success", target: str = "/") -> RedirectResponse:
    # 操作结果通过查询参数带回首页，页面顶部显示提示条。
    query = urlencode({"notice": message, "level": level})
    return RedirectResponse(f"{target}?{query}", status_code=303)


def require_superadmin(request: Request, message: str) -> RedirectResponse | None:
    # 普通账号不允许碰系统级设置和后台账号管理。
    if not is_superadmin(request):
        return redirect_with_notice(message, level="error")
    return None


def build_wallet_backup_csv(wallets: list[WalletWatch]) -> bytes:
    # 网页备份只导出当前账号自己的钱包地址、备注和开关状态，不携带敏感配置。
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["备注别名", "钱包地址", "监控状态", "添加时间"])
    for wallet in wallets:
        writer.writerow(
            [
                wallet.alias,
                wallet.address,
                "启用" if wallet.enabled else "暂停",
                wallet.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")


def wallet_query_for_user(user_id: int):
    # 钱包隔离的核心查询：每个账号只拿自己的钱包列表。
    return select(WalletWatch).where(WalletWatch.owner_user_id == user_id)


def event_query_for_user(user_id: int):
    # 事件记录也按账号隔离，避免朋友之间互相看到彼此的监控结果。
    return select(ChainEvent).where(ChainEvent.owner_user_id == user_id)


def get_owned_wallet(session, request: Request, wallet_id: int) -> WalletWatch | None:
    # 所有钱包操作都必须校验归属，普通账号绝不能操作别人数据。
    row = session.get(WalletWatch, wallet_id)
    if row is None:
        return None
    if row.owner_user_id != current_user_id(request):
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
            bootstrap_user_settings(session, user.id)
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
    # 首页负责展示当前账号的独立钱包、事件、通知设置，以及总管理员专属功能。
    if not is_authenticated(request):
        return login_redirect()

    user_id = current_user_id(request)
    with session_scope() as session:
        wallets = session.scalars(wallet_query_for_user(user_id).order_by(WalletWatch.created_at.desc())).all()
        events = session.scalars(
            event_query_for_user(user_id).order_by(ChainEvent.detected_at.desc()).limit(50)
        ).all()
        admin_users = session.scalars(select(AdminUser).order_by(AdminUser.created_at.asc())).all() if is_superadmin(request) else []
        state = ensure_state(session)
        user_settings = get_user_runtime_settings(session, user_id)
        system_settings = get_system_runtime_settings(session) if is_superadmin(request) else {}
        total_events = session.scalar(
            select(func.count()).select_from(ChainEvent).where(ChainEvent.owner_user_id == user_id)
        ) or 0

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
            "events": events,
            "admin_users": admin_users,
            "admin_user_passwords": admin_user_passwords,
            "state": state,
            "user_settings": user_settings,
            "system_settings": system_settings,
            "total_events": total_events,
            "active_wallets": sum(1 for wallet in wallets if wallet.enabled),
            "notice": request.query_params.get("notice", ""),
            "level": request.query_params.get("level", "success"),
            "current_username": request.session.get("username", ""),
            "current_is_superadmin": is_superadmin(request),
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
        user_settings = get_user_runtime_settings(session, user_id)

    return JSONResponse(
        {
            "monitor_status": state.monitor_status,
            "last_scanned_block": state.last_scanned_block,
            "last_seen_head": state.last_seen_head,
            "last_error": state.last_error,
            "wallet_count": wallets,
            "active_wallet_count": active_wallets,
            "event_count": events,
            "threshold_tao": user_settings.get("large_transfer_threshold_tao"),
            "server_online": True,
            "events": [
                {
                    "id": row.id,
                    "block_number": row.block_number,
                    "amount_tao": row.amount_tao,
                    "message": row.message,
                    "detected_at": row.detected_at.isoformat(),
                }
                for row in latest
            ],
        }
    )


@app.post("/wallets")
async def create_wallet(request: Request, address: str = Form(...), alias: str = Form(...)) -> RedirectResponse:
    # 新增钱包时写入当前登录账号自己的钱包空间。
    if not is_authenticated(request):
        return login_redirect()

    payload = WalletCreate(address=address.strip(), alias=alias.strip())
    user_id = current_user_id(request)
    with session_scope() as session:
        existing = session.scalar(
            select(WalletWatch).where(
                WalletWatch.owner_user_id == user_id,
                WalletWatch.address == payload.address,
            )
        )
        if existing:
            return redirect_with_notice("这个钱包地址已经存在于当前账号", level="error")
        session.add(WalletWatch(owner_user_id=user_id, address=payload.address, alias=payload.alias, enabled=True))

    await monitor.restart()
    return redirect_with_notice("钱包已添加")


@app.post("/wallets/{wallet_id}/toggle")
async def toggle_wallet(request: Request, wallet_id: int) -> RedirectResponse:
    # 钱包支持临时暂停，不需要删除，也只影响当前账号自己的监控。
    if not is_authenticated(request):
        return login_redirect()

    with session_scope() as session:
        row = get_owned_wallet(session, request, wallet_id)
        if row is None:
            return redirect_with_notice("钱包不存在或不属于当前账号", level="error")
        row.enabled = not row.enabled
        label = f"{row.alias} 已{'启用' if row.enabled else '暂停'}监控"

    await monitor.restart()
    return redirect_with_notice(label)


@app.post("/wallets/{wallet_id}/delete")
async def delete_wallet(request: Request, wallet_id: int) -> RedirectResponse:
    # 删除钱包后，监听器也会同步重载配置。
    if not is_authenticated(request):
        return login_redirect()

    with session_scope() as session:
        row = get_owned_wallet(session, request, wallet_id)
        if row is None:
            return redirect_with_notice("钱包不存在或不属于当前账号", level="error")
        label = f"{row.alias} 已删除"
        session.delete(row)

    await monitor.restart()
    return redirect_with_notice(label)


@app.post("/settings/system")
async def save_system_settings(request: Request) -> RedirectResponse:
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
        poll_interval_seconds=int(form.get("poll_interval_seconds", 6)),
        finality_lag_blocks=int(form.get("finality_lag_blocks", 1)),
    )

    with session_scope() as session:
        update_system_runtime_settings(session, payload)

    await monitor.restart()
    return redirect_with_notice("系统设置已保存")


@app.post("/settings/notification")
async def save_notification_settings(request: Request) -> RedirectResponse:
    # 每个账号都可以单独保存自己的 TG 和大额阈值，不会影响别人。
    if not is_authenticated(request):
        return login_redirect()

    form = await request.form()
    payload = UserNotificationSettingsUpdate(
        large_transfer_threshold_tao=float(form.get("large_transfer_threshold_tao", 5)),
        telegram_bot_token=str(form.get("telegram_bot_token", "")).strip(),
        telegram_chat_id=str(form.get("telegram_chat_id", "")).strip(),
    )

    with session_scope() as session:
        update_user_runtime_settings(session, current_user_id(request), payload)

    await monitor.restart()
    return redirect_with_notice("当前账号的通知设置已保存")


@app.post("/settings/test-telegram")
async def test_telegram(request: Request) -> RedirectResponse:
    # 发送当前账号自己的测试消息，用来验证 TG 参数。
    if not is_authenticated(request):
        return login_redirect()

    with session_scope() as session:
        runtime = get_user_runtime_settings(session, current_user_id(request))

    token = str(runtime.get("telegram_bot_token", ""))
    chat_id = str(runtime.get("telegram_chat_id", ""))
    if not token or not chat_id:
        return redirect_with_notice("请先保存当前账号的 Telegram Bot Token 和 Chat ID", level="error")

    try:
        sent = await notifier.send_message(
            token=token,
            chat_id=chat_id,
            text="<b>TAO Monitor</b>\n当前账号的 Telegram 测试消息发送成功。",
        )
    except Exception:
        return redirect_with_notice("Telegram 测试消息发送失败，请检查机器人参数", level="error")

    if not sent:
        return redirect_with_notice("Telegram 测试消息发送失败，请检查机器人参数", level="error")
    return redirect_with_notice("Telegram 测试消息已发送")


@app.post("/admin-users")
async def create_admin_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
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
        return redirect_with_notice("用户名至少需要 3 个字符", level="error")
    if len(normalized_password) < 6:
        return redirect_with_notice("密码至少需要 6 个字符", level="error")

    with session_scope() as session:
        existing = session.scalar(select(AdminUser).where(AdminUser.username == normalized_username))
        if existing:
            return redirect_with_notice("该用户名已经存在", level="error")
        new_user = AdminUser(
            username=normalized_username,
            password_hash=hash_password(normalized_password),
            password_ciphertext=encrypt_password_for_display(normalized_password),
            is_superadmin=False,
        )
        session.add(new_user)
        session.flush()
        bootstrap_user_settings(session, new_user.id)

    return redirect_with_notice(f"账号 {normalized_username} 已创建")


@app.post("/admin-users/{user_id}/delete")
async def delete_admin_user(request: Request, user_id: int) -> RedirectResponse:
    # 删除普通账号时，同时清掉该账号自己的钱包、事件和通知配置。
    if not is_authenticated(request):
        return login_redirect()
    forbidden = require_superadmin(request, "只有总管理员可以删除账号")
    if forbidden is not None:
        return forbidden

    with session_scope() as session:
        user = session.get(AdminUser, user_id)
        if user is None:
            return redirect_with_notice("账号不存在", level="error")
        if user.is_superadmin:
            return redirect_with_notice("总管理员账号不能在这里删除", level="error")

        wallet_rows = session.scalars(select(WalletWatch).where(WalletWatch.owner_user_id == user.id)).all()
        event_rows = session.scalars(select(ChainEvent).where(ChainEvent.owner_user_id == user.id)).all()
        settings_row = session.get(UserSetting, user.id)

        for row in wallet_rows:
            session.delete(row)
        for row in event_rows:
            session.delete(row)
        if settings_row:
            session.delete(settings_row)

        label = f"账号 {user.username} 已删除"
        session.delete(user)

    await monitor.restart()
    return redirect_with_notice(label)


@app.get("/backups/wallets/export")
async def export_wallet_backup(request: Request):
    # 网页备份直接下载当前账号的钱包清单，方便保存到本地，不暴露系统敏感配置。
    if not is_authenticated(request):
        return login_redirect()

    user_id = current_user_id(request)
    with session_scope() as session:
        wallets = session.scalars(wallet_query_for_user(user_id).order_by(WalletWatch.created_at.asc())).all()

    filename = f"tao-wallet-backup-{request.session.get('username', 'user')}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.csv"
    payload = build_wallet_backup_csv(wallets)
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from starlette.middleware.sessions import SessionMiddleware

from app.config import BASE_DIR, get_settings
from app.database import Base, engine, session_scope
from app.models import AdminUser, ChainEvent, WalletWatch
from app.schemas import SettingsUpdate, WalletCreate
from app.services.auth import authenticate_user, bootstrap_admin_user, hash_password
from app.services.settings_service import bootstrap_settings, get_runtime_settings, update_runtime_settings
from app.services.subtensor_monitor import SubtensorMonitor, ensure_state
from app.services.telegram import TelegramNotifier


# 统一日志格式，方便部署后直接查 systemd 日志。
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# 全局单例：监听器、TG 发送器、配置对象、模板引擎。
monitor = SubtensorMonitor()
notifier = TelegramNotifier()
app_settings = get_settings()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 服务启动时初始化数据库、默认设置和总管理员账号。
    Base.metadata.create_all(bind=engine)
    with session_scope() as session:
        bootstrap_settings(session)
        bootstrap_admin_user(session)
        ensure_state(session)
    await monitor.start()
    try:
        yield
    finally:
        await monitor.stop()


app = FastAPI(title="TAO Monitor", lifespan=lifespan)
# 使用 SessionMiddleware 保存网页登录态。
app.add_middleware(SessionMiddleware, secret_key=app_settings.secret_key, same_site="lax")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


def is_authenticated(request: Request) -> bool:
    # 登录成功后会在 session 里写入 authenticated 标记。
    return bool(request.session.get("authenticated"))


def login_redirect() -> RedirectResponse:
    # 未登录时统一跳回登录页。
    return RedirectResponse("/login", status_code=303)


def is_superadmin(request: Request) -> bool:
    # 只有总管理员才允许管理其他后台账号。
    return bool(request.session.get("is_superadmin"))


def redirect_with_notice(message: str, level: str = "success", target: str = "/") -> RedirectResponse:
    # 操作结果通过 URL 参数带回首页，页面顶部显示提示条。
    query = urlencode({"notice": message, "level": level})
    return RedirectResponse(f"{target}?{query}", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # 已登录就直接回首页，避免重复显示登录页。
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
    # 登录成功后把用户身份和权限写进 session。
    with session_scope() as session:
        user = authenticate_user(session, username=username, password=password)
    if user is None:
        return RedirectResponse("/login?error=%E7%94%A8%E6%88%B7%E5%90%8D%E6%88%96%E5%AF%86%E7%A0%81%E4%B8%8D%E6%AD%A3%E7%A1%AE", status_code=303)
    request.session["authenticated"] = True
    request.session["username"] = user.username
    request.session["is_superadmin"] = user.is_superadmin
    return redirect_with_notice("登录成功")


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    # 退出登录时直接清空 session。
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    # 首页汇总钱包、事件、账号和当前监听状态。
    if not is_authenticated(request):
        return login_redirect()
    with session_scope() as session:
        wallets = session.scalars(select(WalletWatch).order_by(WalletWatch.created_at.desc())).all()
        events = session.scalars(select(ChainEvent).order_by(ChainEvent.detected_at.desc()).limit(50)).all()
        admin_users = session.scalars(select(AdminUser).order_by(AdminUser.created_at.asc())).all()
        state = ensure_state(session)
        runtime = get_runtime_settings(session)
        total_events = session.scalar(select(func.count()).select_from(ChainEvent)) or 0
        active_wallets = session.scalar(
            select(func.count()).select_from(WalletWatch).where(WalletWatch.enabled.is_(True))
        ) or 0
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "wallets": wallets,
            "events": events,
            "admin_users": admin_users,
            "state": state,
            "settings": runtime,
            "total_events": total_events,
            "active_wallets": active_wallets,
            "notice": request.query_params.get("notice", ""),
            "level": request.query_params.get("level", "success"),
            "current_username": request.session.get("username", ""),
            "current_is_superadmin": is_superadmin(request),
        },
    )


@app.get("/healthz")
async def healthcheck() -> dict[str, str]:
    # 供反向代理或监控系统做基础健康检查。
    return {"status": "ok"}


@app.get("/api/state")
async def api_state(request: Request) -> JSONResponse:
    # 给前端轮询使用的轻量状态接口。
    if not is_authenticated(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    with session_scope() as session:
        state = ensure_state(session)
        wallets = session.scalar(select(func.count()).select_from(WalletWatch)) or 0
        active_wallets = session.scalar(
            select(func.count()).select_from(WalletWatch).where(WalletWatch.enabled.is_(True))
        ) or 0
        events = session.scalar(select(func.count()).select_from(ChainEvent)) or 0
        latest = session.scalars(select(ChainEvent).order_by(desc(ChainEvent.detected_at)).limit(10)).all()
        runtime = get_runtime_settings(session)
    return JSONResponse(
        {
            "monitor_status": state.monitor_status,
            "last_scanned_block": state.last_scanned_block,
            "last_seen_head": state.last_seen_head,
            "last_error": state.last_error,
            "wallet_count": wallets,
            "active_wallet_count": active_wallets,
            "event_count": events,
            "threshold_tao": runtime.get("large_transfer_threshold_tao"),
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
    # 新增要监控的钱包地址和别名。
    if not is_authenticated(request):
        return login_redirect()
    payload = WalletCreate(address=address.strip(), alias=alias.strip())
    with session_scope() as session:
        existing = session.scalar(select(WalletWatch).where(WalletWatch.address == payload.address))
        if existing:
            return redirect_with_notice("这个钱包地址已经存在", level="error")
        session.add(WalletWatch(address=payload.address, alias=payload.alias, enabled=True))
    await monitor.restart()
    return redirect_with_notice("钱包已添加")


@app.post("/wallets/{wallet_id}/toggle")
async def toggle_wallet(request: Request, wallet_id: int) -> RedirectResponse:
    # 钱包可以临时暂停，不必删除。
    if not is_authenticated(request):
        return login_redirect()
    with session_scope() as session:
        row = session.get(WalletWatch, wallet_id)
        if row is None:
            return redirect_with_notice("钱包不存在", level="error")
        row.enabled = not row.enabled
        label = f"{row.alias} 已{'启用' if row.enabled else '暂停'}监控"
    await monitor.restart()
    return redirect_with_notice(label)


@app.post("/wallets/{wallet_id}/delete")
async def delete_wallet(request: Request, wallet_id: int) -> RedirectResponse:
    # 删除钱包后，也会触发监听器重新载入配置。
    if not is_authenticated(request):
        return login_redirect()
    with session_scope() as session:
        row = session.get(WalletWatch, wallet_id)
        if row is None:
            return redirect_with_notice("钱包不存在", level="error")
        label = f"{row.alias} 已删除"
        session.delete(row)
    await monitor.restart()
    return redirect_with_notice(label)


@app.post("/settings")
async def save_settings(request: Request) -> RedirectResponse:
    # 保存页面上的运行配置，下一轮扫描自动读取。
    if not is_authenticated(request):
        return login_redirect()
    form = await request.form()
    payload = SettingsUpdate(
        subtensor_ws_url=str(form.get("subtensor_ws_url", "")).strip(),
        network_name=str(form.get("network_name", "")).strip(),
        large_transfer_threshold_tao=float(form.get("large_transfer_threshold_tao", 5)),
        telegram_bot_token=str(form.get("telegram_bot_token", "")).strip(),
        telegram_chat_id=str(form.get("telegram_chat_id", "")).strip(),
        poll_interval_seconds=int(form.get("poll_interval_seconds", 6)),
        finality_lag_blocks=int(form.get("finality_lag_blocks", 1)),
    )
    with session_scope() as session:
        update_runtime_settings(session, payload)
    await monitor.restart()
    return redirect_with_notice("运行设置已保存")


@app.post("/settings/test-telegram")
async def test_telegram(request: Request) -> RedirectResponse:
    # 单独发一条测试消息，用来验证 TG 参数是否可用。
    if not is_authenticated(request):
        return login_redirect()
    with session_scope() as session:
        runtime = get_runtime_settings(session)
    token = runtime.get("telegram_bot_token", "")
    chat_id = runtime.get("telegram_chat_id", "")
    if not token or not chat_id:
        return redirect_with_notice("请先保存 Telegram Bot Token 和 Chat ID", level="error")
    await notifier.send_message(
        token=token,
        chat_id=chat_id,
        text="<b>TAO Monitor</b>\nTelegram 测试消息发送成功。",
    )
    return redirect_with_notice("Telegram 测试消息已发送")


@app.post("/admin-users")
async def create_admin_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> RedirectResponse:
    # 总管理员新增普通后台账号，方便朋友共同使用网页。
    if not is_authenticated(request):
        return login_redirect()
    if not is_superadmin(request):
        return redirect_with_notice("只有总管理员可以添加账号", level="error")

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
        session.add(
            AdminUser(
                username=normalized_username,
                password_hash=hash_password(normalized_password),
                is_superadmin=False,
            )
        )
    return redirect_with_notice(f"账号 {normalized_username} 已创建")


@app.post("/admin-users/{user_id}/delete")
async def delete_admin_user(request: Request, user_id: int) -> RedirectResponse:
    # 普通账号可以删除，但总管理员账号在网页里受保护。
    if not is_authenticated(request):
        return login_redirect()
    if not is_superadmin(request):
        return redirect_with_notice("只有总管理员可以删除账号", level="error")

    with session_scope() as session:
        user = session.get(AdminUser, user_id)
        if user is None:
            return redirect_with_notice("账号不存在", level="error")
        if user.is_superadmin:
            return redirect_with_notice("总管理员账号不能在这里删除", level="error")
        label = f"账号 {user.username} 已删除"
        session.delete(user)
    return redirect_with_notice(label)


@app.post("/monitor/restart")
async def restart_monitor(request: Request) -> RedirectResponse:
    # 手动重新载入监听器，适合改完配置后立即生效。
    if not is_authenticated(request):
        return login_redirect()
    await monitor.restart()
    return redirect_with_notice("监听器已重新载入")

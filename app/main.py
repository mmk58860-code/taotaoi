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


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

monitor = SubtensorMonitor()
notifier = TelegramNotifier()
app_settings = get_settings()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
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
app.add_middleware(SessionMiddleware, secret_key=app_settings.secret_key, same_site="lax")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


def login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def is_superadmin(request: Request) -> bool:
    return bool(request.session.get("is_superadmin"))


def redirect_with_notice(message: str, level: str = "success", target: str = "/") -> RedirectResponse:
    query = urlencode({"notice": message, "level": level})
    return RedirectResponse(f"{target}?{query}", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse | RedirectResponse:
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
    with session_scope() as session:
        user = authenticate_user(session, username=username, password=password)
    if user is None:
        return RedirectResponse("/login?error=Invalid+username+or+password", status_code=303)
    request.session["authenticated"] = True
    request.session["username"] = user.username
    request.session["is_superadmin"] = user.is_superadmin
    return redirect_with_notice("Login successful")


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse | RedirectResponse:
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
    return {"status": "ok"}


@app.get("/api/state")
async def api_state(request: Request) -> JSONResponse:
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
    if not is_authenticated(request):
        return login_redirect()
    payload = WalletCreate(address=address.strip(), alias=alias.strip())
    with session_scope() as session:
        existing = session.scalar(select(WalletWatch).where(WalletWatch.address == payload.address))
        if existing:
            return redirect_with_notice("Wallet already exists", level="error")
        session.add(WalletWatch(address=payload.address, alias=payload.alias, enabled=True))
    await monitor.restart()
    return redirect_with_notice("Wallet added")


@app.post("/wallets/{wallet_id}/toggle")
async def toggle_wallet(request: Request, wallet_id: int) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect()
    with session_scope() as session:
        row = session.get(WalletWatch, wallet_id)
        if row is None:
            return redirect_with_notice("Wallet not found", level="error")
        row.enabled = not row.enabled
        label = f"{row.alias} {'enabled' if row.enabled else 'paused'}"
    await monitor.restart()
    return redirect_with_notice(label)


@app.post("/wallets/{wallet_id}/delete")
async def delete_wallet(request: Request, wallet_id: int) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect()
    with session_scope() as session:
        row = session.get(WalletWatch, wallet_id)
        if row is None:
            return redirect_with_notice("Wallet not found", level="error")
        label = f"{row.alias} deleted"
        session.delete(row)
    await monitor.restart()
    return redirect_with_notice(label)


@app.post("/settings")
async def save_settings(request: Request) -> RedirectResponse:
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
    return redirect_with_notice("Settings saved")


@app.post("/settings/test-telegram")
async def test_telegram(request: Request) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect()
    with session_scope() as session:
        runtime = get_runtime_settings(session)
    token = runtime.get("telegram_bot_token", "")
    chat_id = runtime.get("telegram_chat_id", "")
    if not token or not chat_id:
        return redirect_with_notice("Save Telegram token and chat id first", level="error")
    await notifier.send_message(
        token=token,
        chat_id=chat_id,
        text="<b>TAO Monitor</b>\nTelegram test message sent successfully.",
    )
    return redirect_with_notice("Telegram test message sent")


@app.post("/admin-users")
async def create_admin_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect()
    if not is_superadmin(request):
        return redirect_with_notice("Only the superadmin can add users", level="error")

    normalized_username = username.strip()
    normalized_password = password.strip()
    if len(normalized_username) < 3:
        return redirect_with_notice("Username must be at least 3 characters", level="error")
    if len(normalized_password) < 6:
        return redirect_with_notice("Password must be at least 6 characters", level="error")

    with session_scope() as session:
        existing = session.scalar(select(AdminUser).where(AdminUser.username == normalized_username))
        if existing:
            return redirect_with_notice("Username already exists", level="error")
        session.add(
            AdminUser(
                username=normalized_username,
                password_hash=hash_password(normalized_password),
                is_superadmin=False,
            )
        )
    return redirect_with_notice(f"User {normalized_username} created")


@app.post("/admin-users/{user_id}/delete")
async def delete_admin_user(request: Request, user_id: int) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect()
    if not is_superadmin(request):
        return redirect_with_notice("Only the superadmin can delete users", level="error")

    with session_scope() as session:
        user = session.get(AdminUser, user_id)
        if user is None:
            return redirect_with_notice("User not found", level="error")
        if user.is_superadmin:
            return redirect_with_notice("The superadmin account cannot be deleted here", level="error")
        label = f"User {user.username} deleted"
        session.delete(user)
    return redirect_with_notice(label)


@app.post("/monitor/restart")
async def restart_monitor(request: Request) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect()
    await monitor.restart()
    return redirect_with_notice("Monitor restarted")

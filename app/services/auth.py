from __future__ import annotations

import hashlib
import hmac
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AdminUser


def hash_password(password: str, salt: str | None = None) -> str:
    # 使用 PBKDF2 生成密码哈希，避免数据库里保存明文密码。
    resolved_salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), resolved_salt.encode("utf-8"), 120_000)
    return f"{resolved_salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    # 取出原盐值重新计算哈希，再做常量时间比较。
    try:
        salt, expected = stored_hash.split("$", 1)
    except ValueError:
        return False
    candidate = hash_password(password, salt)
    return hmac.compare_digest(candidate, f"{salt}${expected}")


def bootstrap_admin_user(session: Session) -> None:
    # 服务第一次启动时，自动确保总管理员账号存在。
    settings = get_settings()
    existing = session.scalar(select(AdminUser).where(AdminUser.username == settings.admin_username))
    if existing is None:
        session.add(
            AdminUser(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                is_superadmin=True,
            )
        )
        session.flush()
        return

    changed = False
    if not existing.is_superadmin:
        existing.is_superadmin = True
        changed = True
    if not verify_password(settings.admin_password, existing.password_hash):
        existing.password_hash = hash_password(settings.admin_password)
        changed = True
    if changed:
        session.flush()


def authenticate_user(session: Session, username: str, password: str) -> AdminUser | None:
    # 登录时按用户名查人，再核对密码哈希。
    user = session.scalar(select(AdminUser).where(AdminUser.username == username))
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user

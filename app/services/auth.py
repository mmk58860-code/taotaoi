from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken
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


def encrypt_password_for_display(password: str) -> str:
    # 为“总管理员可回看密码”单独保留一份可解密密文。
    cipher = _password_cipher()
    return cipher.encrypt(password.encode("utf-8")).decode("utf-8")


def decrypt_password_for_display(ciphertext: str) -> str:
    # 历史账号如果没有可回显密文，就返回空字符串给前端做兼容提示。
    if not ciphertext:
        return ""
    cipher = _password_cipher()
    try:
        return cipher.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return ""


def bootstrap_admin_user(session: Session) -> None:
    # 服务第一次启动时，自动确保总管理员账号存在。
    settings = get_settings()
    existing = session.scalar(select(AdminUser).where(AdminUser.username == settings.admin_username))
    if existing is None:
        session.add(
            AdminUser(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                password_ciphertext=encrypt_password_for_display(settings.admin_password),
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
        existing.password_ciphertext = encrypt_password_for_display(settings.admin_password)
        changed = True
    elif not existing.password_ciphertext:
        existing.password_ciphertext = encrypt_password_for_display(settings.admin_password)
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


def _password_cipher() -> Fernet:
    # 用 SECRET_KEY 派生一个固定密钥，避免额外维护第二套密码加密配置。
    settings = get_settings()
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))

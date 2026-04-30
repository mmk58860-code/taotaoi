from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


# 所有 ORM 模型都继承这个基类，数据库结构交给 Alembic 迁移脚本管理。
class Base(DeclarativeBase):
    pass


settings = get_settings()

# 正式部署统一使用 PostgreSQL，避免 SQLite 在并发写入、备份和后续扩展上的限制。
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@contextmanager
def session_scope() -> Session:
    # 统一的数据库会话上下文：成功就提交，失败就回滚。
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

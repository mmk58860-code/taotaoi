from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import BASE_DIR, get_settings


# 所有 ORM 模型都继承这个基类。
class Base(DeclarativeBase):
    pass


settings = get_settings()

if settings.database_url.startswith("sqlite:///"):
    # SQLite 模式下，先确保数据库目录存在。
    db_path = BASE_DIR / settings.database_url.removeprefix("sqlite:///")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

# 创建数据库引擎；SQLite 需要关闭跨线程限制。
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
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

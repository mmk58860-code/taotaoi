from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# 项目根目录，供数据库、模板、静态文件统一引用。
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # 统一从 .env 读取运行配置，未知字段会被忽略，方便后续扩展。
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8080, alias="APP_PORT")
    database_url: str = Field(
        default="postgresql+psycopg2://taomonitor:change-this-password@127.0.0.1:5432/tao_monitor",
        alias="DATABASE_URL",
    )
    subtensor_ws_url: str = Field(
        default="wss://entrypoint-finney.opentensor.ai:443",
        alias="SUBTENSOR_WS_URL",
    )
    network_name: str = Field(default="finney", alias="NETWORK_NAME")
    poll_interval_seconds: int = Field(default=2, alias="POLL_INTERVAL_SECONDS")
    finality_lag_blocks: int = Field(default=0, alias="FINALITY_LAG_BLOCKS")
    large_transfer_threshold_tao: float = Field(default=5.0, alias="LARGE_TRANSFER_THRESHOLD_TAO")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    secret_key: str = Field(default="change-me", alias="SECRET_KEY")
    admin_username: str = Field(default="admin", alias="ADMIN_USERNAME")
    admin_password: str = Field(default="change-this-password", alias="ADMIN_PASSWORD")
    cleanup_enabled: bool = Field(default=True, alias="CLEANUP_ENABLED")
    cleanup_time: str = Field(default="04:00", alias="CLEANUP_TIME")
    cleanup_retention_days: int = Field(default=3, alias="CLEANUP_RETENTION_DAYS")

    @property
    def sqlite_path(self) -> Path | None:
        # 如果当前数据库是 SQLite，就把连接串转换成真实文件路径。
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            return BASE_DIR / self.database_url.removeprefix(prefix)
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # 配置对象只实例化一次，避免每次请求重复读取环境变量。
    return Settings()

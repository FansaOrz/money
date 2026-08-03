"""应用配置模块。

通过环境变量（前缀 MONEY_）或 .env 文件覆盖默认值。
默认使用 SQLite，便于本地开发；生产可切换 PostgreSQL。
"""

from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置项。"""

    model_config = SettingsConfigDict(
        env_prefix="MONEY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "money-api"
    debug: bool = False

    # 数据库连接串。
    # SQLite（默认开发）: sqlite:///./money.db
    # PostgreSQL（生产）: postgresql+psycopg://user:password@host:5432/money
    database_url: str = "sqlite:///./money.db"

    # 允许跨域的前端来源
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # 导入解析结果仅保留在内存中，未确认的预览不落盘
    import_session_ttl_minutes: int = 30

    # 启动时是否自动创建数据表（MVP 阶段免迁移）
    auto_create_tables: bool = True

    # A 股研究数据层：日线 Parquet 数据湖目录（raw/qfq 分区存放）。
    # DuckDB 研究仓库（warehouse）默认也落在该目录下（research.duckdb + year=YYYY 分区）。
    research_data_dir: str = "./data/research"
    # 研究数据仓库 DuckDB 数据库文件路径（MONEY_RESEARCH_DB 可覆盖）
    research_db: str = "./data/research/research.duckdb"
    # 同步日线时每次批量处理的股票数量，避免长时间占用网络
    research_sync_batch_size: int = 200

    # 新闻事件分析在后台调度器中执行，页面请求只读取本地结果。
    news_analysis_enabled: bool = True
    news_analysis_lookback_days: int = 30
    news_analysis_batch_size: int = 100

    # 可选 OpenAI-compatible 大模型。未开启或调用失败时自动使用保守规则分析。
    news_llm_enabled: bool = False
    news_llm_base_url: str = "https://api.openai.com/v1"
    news_llm_api_key: SecretStr | None = None
    news_llm_model: str = ""
    news_llm_timeout_seconds: int = 30

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_cors_origins(cls, value: object) -> object:
        """支持逗号分隔的字符串写法，例如：
        MONEY_CORS_ORIGINS="http://localhost:5173,https://example.com"
        """
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """返回缓存的配置单例，测试可通过清缓存重新加载。"""
    return Settings()

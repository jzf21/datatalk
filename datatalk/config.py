"""Typed configuration loaded from environment / .env file."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration for DataTalk.

    Values are read from environment variables or a local ``.env`` file.
    See ``.env.example`` for the full list.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # OpenAI (or any OpenAI-compatible endpoint, e.g. Nebius Token Factory)
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    # Base URL for the API. Empty = OpenAI's default. Set to a compatible
    # provider's URL (e.g. https://api.tokenfactory.nebius.com/v1/) to use it.
    openai_base_url: str = Field(default="", alias="OPENAI_BASE_URL")
    openai_model: str = Field(default="gpt-4.1", alias="OPENAI_MODEL")
    openai_embed_model: str = Field(
        default="text-embedding-3-small", alias="OPENAI_EMBED_MODEL"
    )

    # ClickHouse
    clickhouse_host: str = Field(default="localhost", alias="CLICKHOUSE_HOST")
    clickhouse_port: int = Field(default=8123, alias="CLICKHOUSE_PORT")
    clickhouse_user: str = Field(default="default", alias="CLICKHOUSE_USER")
    clickhouse_password: str = Field(default="", alias="CLICKHOUSE_PASSWORD")
    clickhouse_database: str = Field(default="default", alias="CLICKHOUSE_DATABASE")
    clickhouse_secure: bool = Field(default=False, alias="CLICKHOUSE_SECURE")

    # Guardrails
    sql_default_limit: int = Field(default=1000, alias="SQL_DEFAULT_LIMIT")
    sql_max_rows: int = Field(default=5000, alias="SQL_MAX_ROWS")
    sql_timeout_seconds: int = Field(default=30, alias="SQL_TIMEOUT_SECONDS")

    # Introspection
    introspect_sample_rows: int = Field(default=3, alias="INTROSPECT_SAMPLE_ROWS")
    introspect_max_tables: int = Field(default=0, alias="INTROSPECT_MAX_TABLES")
    # Comma-separated database allowlist. Empty = every non-system database.
    # Strongly recommended when the server has many databases (keeps the schema
    # context small, cheap, and focused).
    introspect_databases: str = Field(default="", alias="INTROSPECT_DATABASES")
    # Comma-separated substrings; any table whose name contains one is skipped
    # (e.g. backup/test/old copies).
    introspect_exclude_table_patterns: str = Field(
        default="", alias="INTROSPECT_EXCLUDE_TABLE_PATTERNS"
    )
    @property
    def introspect_database_list(self) -> list[str]:
        return [d.strip() for d in self.introspect_databases.split(",") if d.strip()]

    @property
    def introspect_exclude_list(self) -> list[str]:
        return [
            p.strip().lower()
            for p in self.introspect_exclude_table_patterns.split(",")
            if p.strip()
        ]

    # Storage
    datatalk_db_path: str = Field(default="datatalk.sqlite3", alias="DATATALK_DB_PATH")

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key and self.openai_api_key != "sk-...")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()

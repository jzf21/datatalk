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
    # The data-documentation agent. It runs rarely, agentically, over a whole
    # warehouse, and its output then lands in every later prompt -- so it is
    # worth a stronger (slower, pricier) model than the per-report loop.
    # Optionally a different endpoint entirely, e.g. reports on a self-hosted
    # model while documentation runs on a frontier one. Empty = reuse the
    # OPENAI_* values above.
    openai_docs_model: str = Field(default="", alias="OPENAI_DOCS_MODEL")
    openai_docs_base_url: str = Field(default="", alias="OPENAI_DOCS_BASE_URL")
    openai_docs_api_key: str = Field(default="", alias="OPENAI_DOCS_API_KEY")
    # The dashboard author. One call per dashboard, and the most structurally
    # demanding one in the pipeline: it must emit a whole grid as a single valid
    # JSON object referencing only real dataset ids and columns. That is exactly
    # where a small open-weights model is weakest, and a malformed reply costs
    # the entire dashboard -- so it is worth pointing at a stronger model even
    # when the query loop stays cheap. Empty = reuse the OPENAI_* values above.
    openai_author_model: str = Field(default="", alias="OPENAI_AUTHOR_MODEL")
    openai_author_base_url: str = Field(default="", alias="OPENAI_AUTHOR_BASE_URL")
    openai_author_api_key: str = Field(default="", alias="OPENAI_AUTHOR_API_KEY")
    # Completion cap for the author/insight calls. 0 = omit the parameter (the
    # provider's default). Worth setting on OpenAI-compatible servers whose
    # default completion cap silently truncates a large dashboard grid.
    openai_author_max_tokens: int = Field(default=0, alias="OPENAI_AUTHOR_MAX_TOKENS")
    # Transport resilience, applied to every OpenAI client this process builds.
    # Retries are the SDK's own (429/5xx/connect errors, with backoff), so a
    # transient limit does not kill a 15-minute report run; the timeout bounds
    # a single hung completion, which would otherwise silence a stream past any
    # proxy's patience (the SDK default is 600s).
    openai_max_retries: int = Field(default=5, alias="OPENAI_MAX_RETRIES")
    openai_timeout_seconds: float = Field(default=120.0, alias="OPENAI_TIMEOUT_SECONDS")
    # Completion cap for the analyze/critique calls, mirroring the author cap
    # above. 0 = omit the parameter.
    openai_analyze_max_tokens: int = Field(default=0, alias="OPENAI_ANALYZE_MAX_TOKENS")

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
    # Postgres holds orgs, users, sessions and all per-org memory. Required at
    # runtime; empty fails fast in the app lifespan rather than at first request.
    database_url: str = Field(default="", alias="DATABASE_URL")
    db_pool_size: int = Field(default=10, alias="DATATALK_DB_POOL_SIZE")
    db_max_overflow: int = Field(default=20, alias="DATATALK_DB_MAX_OVERFLOW")
    db_echo: bool = Field(default=False, alias="DATATALK_DB_ECHO")
    # Auto-run `alembic upgrade head` on startup. Safe only single-process:
    # two workers racing `upgrade head` can corrupt alembic_version.
    db_auto_migrate: bool = Field(default=False, alias="DATATALK_DB_AUTO_MIGRATE")
    # Legacy SQLite path -- read only by `datatalk-import-sqlite`.
    datatalk_db_path: str = Field(default="datatalk.sqlite3", alias="DATATALK_DB_PATH")

    # Secrets
    # urlsafe-base64 Fernet key(s) encrypting per-org ClickHouse passwords.
    # Comma-separated for rotation: the first encrypts, all decrypt.
    datatalk_secret_key: str = Field(default="", alias="DATATALK_SECRET_KEY")

    # Auth / sessions
    cookie_name: str = Field(default="dt_session", alias="DATATALK_COOKIE_NAME")
    # Marks the session cookie Secure. MUST stay false on plain-HTTP dev or the
    # browser silently drops the cookie and every login 200s then 401s.
    cookie_secure: bool = Field(default=False, alias="DATATALK_COOKIE_SECURE")
    session_ttl_days: int = Field(default=14, alias="DATATALK_SESSION_TTL_DAYS")
    # Anyone may sign up and create an org. Fine for local development; with it
    # on, a stranger gets an org and spends this deployment's OpenAI key.
    allow_open_signup: bool = Field(default=True, alias="DATATALK_ALLOW_OPEN_SIGNUP")

    # Bootstrap (idempotent, applied on startup when set)
    bootstrap_org_name: str = Field(default="", alias="DATATALK_BOOTSTRAP_ORG_NAME")
    bootstrap_admin_email: str = Field(default="", alias="DATATALK_BOOTSTRAP_ADMIN_EMAIL")
    bootstrap_admin_password: str = Field(
        default="", alias="DATATALK_BOOTSTRAP_ADMIN_PASSWORD"
    )

    # Memory retrieval
    # Upper bound on suggestion rows scored in Python per retrieval. An OOM
    # guard a single org cannot defeat by adding suggestions.
    memory_max_candidates: int = Field(default=5000, alias="DATATALK_MEMORY_MAX_CANDIDATES")

    # Web / CORS
    # Comma-separated browser origins allowed to call the API (the Next.js
    # frontend runs on its own origin). Empty = no CORS headers at all.
    cors_allow_origins: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000",
        alias="DATATALK_CORS_ORIGINS",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [
            o.strip().rstrip("/")
            for o in self.cors_allow_origins.split(",")
            if o.strip()
        ]

    @property
    def has_openai(self) -> bool:
        return bool(self.openai_api_key and self.openai_api_key != "sk-...")

    @property
    def docs_model(self) -> str:
        return self.openai_docs_model or self.openai_model

    @property
    def docs_openai_settings(self) -> "Settings":
        """Settings whose ``openai_*`` fields address the documentation endpoint.

        Returns ``self`` when nothing is overridden, so ``clients.openai_for``
        hands back the very same shared client and nothing extra is cached.
        """
        if not (self.openai_docs_base_url or self.openai_docs_api_key):
            return self
        return self.model_copy(
            update={
                "openai_api_key": self.openai_docs_api_key or self.openai_api_key,
                "openai_base_url": self.openai_docs_base_url or self.openai_base_url,
            }
        )

    @property
    def author_model(self) -> str:
        return self.openai_author_model or self.openai_model

    @property
    def author_openai_settings(self) -> "Settings":
        """Settings whose ``openai_*`` fields address the author endpoint.

        Returns ``self`` when nothing is overridden, so ``clients.openai_for``
        hands back the very same shared client and nothing extra is cached.
        """
        if not (self.openai_author_base_url or self.openai_author_api_key):
            return self
        return self.model_copy(
            update={
                "openai_api_key": self.openai_author_api_key or self.openai_api_key,
                "openai_base_url": (
                    self.openai_author_base_url or self.openai_base_url
                ),
            }
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()

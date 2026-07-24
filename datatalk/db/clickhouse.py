"""ClickHouse connection factory built on the official clickhouse-connect client."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from datatalk.config import Settings, get_settings


def create_client(settings: Settings | None = None) -> Client:
    """Create a new ClickHouse client from settings.

    The caller owns the returned client and should close it when done, or use
    :func:`get_client` for a cached shared instance.
    """
    settings = settings or get_settings()
    return clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_database,
        secure=settings.clickhouse_secure,
        query_limit=0,  # we enforce our own limits in the executor
    )


@lru_cache(maxsize=1)
def get_client() -> Client:
    """Return a cached, shared ClickHouse client."""
    return create_client()


def ping(client: Client | None = None) -> dict[str, Any]:
    """Verify connectivity and return basic server info.

    Raises whatever the driver raises on failure; callers should catch and
    present it to the user (this is used by the connection-checkout flow).
    """
    client = client or get_client()
    version = client.command("SELECT version()")
    current_db = client.command("SELECT currentDatabase()")
    return {"version": str(version), "database": str(current_db)}

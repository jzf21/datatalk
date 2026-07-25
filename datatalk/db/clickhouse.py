"""ClickHouse connection factory built on the official clickhouse-connect client."""

from __future__ import annotations

from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from datatalk.config import Settings


def create_client(settings: Settings) -> Client:
    """Create a new ClickHouse client from settings.

    The caller owns the returned client. Prefer
    :func:`datatalk.clients.clickhouse_for`, which caches one client per
    connection fingerprint; use this directly only for throwaway clients (e.g.
    testing a candidate connection) that must not enter the registry.
    """
    return clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_database,
        secure=settings.clickhouse_secure,
        query_limit=0,  # we enforce our own limits in the executor
        # Without this the driver pins a server-side session id per client, and
        # ClickHouse serializes concurrent queries on one session
        # (SESSION_IS_LOCKED). Agents run in parallel worker threads sharing a
        # cached client, so concurrent queries are the normal case.
        autogenerate_session_id=False,
    )


def ping(client: Client) -> dict[str, Any]:
    """Verify connectivity and return basic server info.

    Raises whatever the driver raises on failure; callers should catch and
    present it to the user (this is used by the connection-checkout flow).
    """
    version = client.command("SELECT version()")
    current_db = client.command("SELECT currentDatabase()")
    return {"version": str(version), "database": str(current_db)}

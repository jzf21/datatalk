"""The Postgres adapter, against a real Postgres.

The suite already requires a live Postgres for the control plane, so pointing a
warehouse at it costs nothing and buys the first end-to-end coverage of an
adapter: connect, introspect, query, cap, and -- the part that matters most --
refuse to write.

Everything lives in a dedicated ``wh_test`` schema so it cannot collide with the
control-plane tables in the same database.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from datatalk.agent.executor import UnsafeSQLError, run_sql
from datatalk.context import SourceRef, TenantContext
from datatalk.warehouse import WarehouseError, WarehouseSpec, catalog, create
from tests.conftest import make_settings

SCHEMA = "wh_test"


@pytest.fixture(scope="module")
def warehouse_spec(engine, database_url) -> WarehouseSpec:
    """A source pointed at the test database's ``wh_test`` schema."""
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        conn.execute(
            text(
                f"""
                CREATE TABLE {SCHEMA}.events (
                    id bigint PRIMARY KEY,
                    kind text NOT NULL,
                    amount numeric(10, 2),
                    at timestamptz
                )
                """
            )
        )
        conn.execute(
            text(f"COMMENT ON TABLE {SCHEMA}.events IS 'product events'")
        )
        conn.execute(text(f"COMMENT ON COLUMN {SCHEMA}.events.kind IS 'event name'"))
        conn.execute(
            text(
                f"INSERT INTO {SCHEMA}.events (id, kind, amount, at) "
                "SELECT g, 'click', g * 1.5, now() FROM generate_series(1, 25) g"
            )
        )
        # A second table, to prove the allowlist and exclude patterns bite.
        conn.execute(text(f"CREATE TABLE {SCHEMA}.events_backup (id bigint)"))

    url = make_url(database_url)
    yield WarehouseSpec(
        type="postgres",
        host=url.host,
        port=url.port or 5432,
        username=url.username,
        password=url.password or "",
        database=url.database,
        introspect_databases=(SCHEMA,),
        sql_max_rows=10,
        sql_timeout_seconds=5,
    )

    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))


@pytest.fixture
def warehouse(warehouse_spec):
    wh = create(warehouse_spec)
    yield wh
    wh.close()


# --- connect and introspect ---------------------------------------------------


def test_ping_reports_version_and_database(warehouse, warehouse_spec):
    info = warehouse.ping()
    assert "PostgreSQL" in info["version"]
    assert info["database"] == warehouse_spec.database


def test_introspection_finds_tables_columns_and_comments(warehouse):
    tables = {t.name: t for t in warehouse.introspect()}

    assert "events" in tables
    events = tables["events"]
    assert events.database == SCHEMA
    assert events.qualified_name == f"{SCHEMA}.events"
    assert events.comment == "product events"
    assert events.engine == "table"

    columns = {c.name: c for c in events.columns}
    assert set(columns) == {"id", "kind", "amount", "at"}
    # format_type, not the raw oid: the model has to write valid casts.
    assert columns["amount"].type == "numeric(10,2)"
    assert columns["at"].type == "timestamp with time zone"
    assert columns["kind"].comment == "event name"


def test_introspection_samples_rows(warehouse):
    events = next(t for t in warehouse.introspect() if t.name == "events")
    assert len(events.sample_rows) == warehouse.spec.introspect_sample_rows
    assert events.sample_rows[0]["kind"] == "click"


def test_the_schema_allowlist_hides_everything_else(warehouse):
    """The control-plane tables live in `public` in this very database."""
    namespaces = {t.database for t in warehouse.introspect()}
    assert namespaces == {SCHEMA}
    names = {t.name for t in warehouse.introspect()}
    assert "orgs" not in names and "users" not in names


def test_exclude_patterns_drop_matching_tables(warehouse_spec):
    import dataclasses

    spec = dataclasses.replace(
        warehouse_spec, introspect_exclude_patterns=("backup",)
    )
    wh = create(spec)
    try:
        names = {t.name for t in wh.introspect()}
        assert "events" in names
        assert "events_backup" not in names
    finally:
        wh.close()


# --- querying -----------------------------------------------------------------


def test_query_returns_columns_and_rows(warehouse):
    result = warehouse.query(
        f"SELECT id, kind FROM {SCHEMA}.events ORDER BY id LIMIT 3",
        timeout_s=5,
        max_rows=100,
    )
    assert result.columns == ["id", "kind"]
    assert result.rows == [[1, "click"], [2, "click"], [3, "click"]]
    assert result.row_count == 3
    assert result.truncated is False


def test_max_rows_caps_and_flags_truncation(warehouse):
    """The cap is enforced by the adapter, not trusted to the model's LIMIT."""
    result = warehouse.query(
        f"SELECT id FROM {SCHEMA}.events ORDER BY id", timeout_s=5, max_rows=5
    )
    assert len(result.rows) == 5
    assert result.truncated is True


def test_a_driver_error_becomes_a_warehouse_error(warehouse):
    """Callers catch one exception type whatever the engine underneath."""
    with pytest.raises(WarehouseError):
        warehouse.query("SELECT * FROM nope.missing", timeout_s=5, max_rows=10)


def test_unreachable_host_fails_at_construction(warehouse_spec):
    import dataclasses

    spec = dataclasses.replace(warehouse_spec, host="nonexistent.invalid", port=9)
    with pytest.raises(WarehouseError):
        create(spec)


# --- read-only, twice over ----------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        f"INSERT INTO {SCHEMA}.events (id, kind) VALUES (999, 'x')",
        f"UPDATE {SCHEMA}.events SET kind = 'y'",
        f"DELETE FROM {SCHEMA}.events",
        f"DROP TABLE {SCHEMA}.events",
        f"SELECT * INTO {SCHEMA}.copied FROM {SCHEMA}.events",
    ],
)
def test_the_guardrail_rejects_writes(sql, warehouse_spec):
    """First line of defence: the statement never reaches the server."""
    ctx = _ctx(warehouse_spec)
    with pytest.raises(UnsafeSQLError):
        run_sql(sql, ctx=ctx, source="pg")


@pytest.mark.parametrize(
    "sql",
    [
        f"INSERT INTO {SCHEMA}.events (id, kind) VALUES (999, 'x')",
        f"UPDATE {SCHEMA}.events SET kind = 'y'",
        f"DELETE FROM {SCHEMA}.events",
        f"DROP TABLE {SCHEMA}.events",
        f"CREATE TABLE {SCHEMA}.sneaky (id int)",
    ],
)
def test_the_server_refuses_writes_with_the_guardrail_bypassed(sql, warehouse):
    """Second line of defence, and the one that does not depend on our parser.

    Called through ``warehouse.query`` directly, skipping ``validate_sql``
    entirely -- this is what a hole in the tokenizer would look like. Every
    session is opened read-only, so the server rejects it regardless.
    """
    with pytest.raises(WarehouseError) as exc:
        warehouse.query(sql, timeout_s=5, max_rows=10)
    assert "read-only" in str(exc.value).lower()


def test_the_table_is_still_intact_after_all_that(warehouse):
    result = warehouse.query(
        f"SELECT count(*) FROM {SCHEMA}.events", timeout_s=5, max_rows=1
    )
    assert result.rows == [[25]]


def test_statement_timeout_is_applied(warehouse):
    """A runaway query must not hold a pooled connection open indefinitely."""
    with pytest.raises(WarehouseError) as exc:
        warehouse.query("SELECT pg_sleep(3)", timeout_s=1, max_rows=1)
    assert "statement timeout" in str(exc.value).lower()


def test_the_timeout_does_not_leak_to_the_next_query(warehouse):
    """SET LOCAL, so it expires with the transaction, not with the connection."""
    with pytest.raises(WarehouseError):
        warehouse.query("SELECT pg_sleep(3)", timeout_s=1, max_rows=1)

    # Same pooled connection, comfortably longer than the previous 1s cap.
    result = warehouse.query("SELECT pg_sleep(2), 1", timeout_s=10, max_rows=1)
    assert result.rows[0][1] == 1


# --- through the context and catalog ------------------------------------------


def _ctx(spec: WarehouseSpec) -> TenantContext:
    from uuid import uuid4

    return TenantContext.from_sources(
        (SourceRef.from_spec(spec, name="pg"),),
        org_id=uuid4(),
        settings=make_settings(),
    )


def test_run_sql_reaches_the_source_through_the_context(warehouse_spec):
    ctx = _ctx(warehouse_spec)
    result = run_sql(f"SELECT id FROM {SCHEMA}.events ORDER BY id", ctx=ctx, source="pg")

    # sql_max_rows on the spec is 10, so the guardrail's cap wins over the count.
    assert len(result.rows) == 10
    assert result.truncated is True


def test_the_catalog_renders_a_postgres_source(warehouse_spec):
    ctx = _ctx(warehouse_spec)
    text_out = catalog.build_catalog(ctx)

    assert "SOURCE pg [postgres]" in text_out
    assert f"{SCHEMA}.events (id, kind, amount, at)" in text_out
    # The dialect hint travels with the catalog, so prompts stay engine-neutral.
    assert "PostgreSQL dialect" in text_out
    assert "schema.table" in text_out


def test_describe_table_returns_types_and_samples(warehouse_spec):
    ctx = _ctx(warehouse_spec)
    detail = catalog.describe_table(ctx, "pg", f"{SCHEMA}.events")

    assert "numeric(10,2)" in detail
    assert "sample rows:" in detail
    assert "product events" in detail


def test_describe_table_accepts_an_unqualified_name(warehouse_spec):
    """The model often has only the bare name the catalog line showed it."""
    ctx = _ctx(warehouse_spec)
    assert "numeric(10,2)" in catalog.describe_table(ctx, "pg", "events")


def test_describe_table_lists_alternatives_for_a_bad_name(warehouse_spec):
    """Recoverable: the model gets told what does exist rather than an error."""
    ctx = _ctx(warehouse_spec)
    out = catalog.describe_table(ctx, "pg", "no_such_table")
    assert "no_such_table" in out
    assert f"{SCHEMA}.events" in out

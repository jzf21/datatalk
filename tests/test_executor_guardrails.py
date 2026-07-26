"""Guardrail tests for the read-only SQL validator (no live DB required)."""

import pytest

from datatalk.agent.executor import (
    UnsafeSQLError,
    ensure_limit,
    validate_sql,
)
from datatalk.warehouse.clickhouse import CLICKHOUSE_DIALECT
from datatalk.warehouse.postgres import POSTGRES_DIALECT


# --- allowed statements ---

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select id, name from jira.issues where status = 'DROP'",  # keyword only in string
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SHOW TABLES",
        "DESCRIBE jira.issues",
        "DESC jira.issues",
        "EXPLAIN SELECT 1",
        "  \n SELECT 1 ;  ",  # trailing semicolon + whitespace ok
        "SELECT `drop` FROM t",  # forbidden word only as quoted identifier
        "SELECT 1 -- DROP TABLE t",  # forbidden word only in a comment
        "SELECT /* DELETE */ 1",  # forbidden word only in a block comment
    ],
)
def test_allows_read_only(sql):
    assert validate_sql(sql)  # should not raise


# --- rejected statements ---

@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN a Int",
        "CREATE TABLE t (a Int) ENGINE = Memory",
        "TRUNCATE TABLE t",
        "SET max_threads = 4",
        "SYSTEM RELOAD CONFIG",
        "SELECT 1; DROP TABLE t",  # multi-statement
        "SELECT 1; SELECT 2",  # multi-statement, both reads
        "GRANT SELECT ON db.* TO user",
        "",
        "   ",
    ],
)
def test_rejects_non_read_only(sql):
    with pytest.raises(UnsafeSQLError):
        validate_sql(sql)


def test_forbidden_hidden_after_comment_then_real_write_is_rejected():
    # The write is real (not commented), so it must be rejected.
    sql = "SELECT 1 -- ok\nUNION ALL SELECT 2; DROP TABLE t"
    with pytest.raises(UnsafeSQLError):
        validate_sql(sql)


# --- limit injection ---

def test_limit_injected_when_missing():
    out = ensure_limit("SELECT * FROM t", 1000)
    assert "LIMIT 1000" in out


def test_limit_not_injected_when_present():
    out = ensure_limit("SELECT * FROM t LIMIT 5", 1000)
    assert out.count("LIMIT") == 1
    assert "LIMIT 5" in out


def test_limit_not_injected_for_show_describe():
    assert "LIMIT" not in ensure_limit("SHOW TABLES", 1000)
    assert "LIMIT" not in ensure_limit("DESCRIBE t", 1000)


def test_limit_detection_ignores_string_literals():
    # 'LIMIT' appears only inside a string, so a real LIMIT should be added.
    out = ensure_limit("SELECT * FROM t WHERE note = 'no LIMIT here'", 1000)
    assert out.strip().endswith("LIMIT 1000")


# --- per-dialect rules --------------------------------------------------------
#
# The allowed leaders are shared, but each engine adds its own unsafe verbs and
# its own quoting. A rule that only exists for ClickHouse leaves the Postgres
# adapter with a weaker guard than the one it advertises.


@pytest.mark.parametrize(
    "sql",
    [
        # Writes and DDL the shared core misses on Postgres.
        "COPY t TO '/tmp/x.csv'",
        "VACUUM FULL t",
        "ANALYZE t",
        "REINDEX TABLE t",
        "CLUSTER t",
        "REFRESH MATERIALIZED VIEW v",
        "COMMENT ON TABLE t IS 'x'",
        "LOCK TABLE t",
        # Procedural / session escapes.
        "DO $$ BEGIN END $$",
        "PREPARE p AS SELECT 1",
        "EXECUTE p",
        "DISCARD ALL",
        "LISTEN chan",
        "NOTIFY chan",
        # Transaction control would let a statement leave the read-only txn.
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        # A write that passes the leader check: SELECT ... INTO creates a table.
        "SELECT * INTO newtable FROM t",
    ],
)
def test_postgres_rejects_its_own_hazards(sql):
    with pytest.raises(UnsafeSQLError):
        validate_sql(sql, POSTGRES_DIALECT)


@pytest.mark.parametrize(
    "sql",
    [
        "ATTACH TABLE t",
        "DETACH TABLE t",
        "OPTIMIZE TABLE t FINAL",
        "SYSTEM RELOAD CONFIG",
        "KILL QUERY WHERE query_id = '1'",
        "UNDROP TABLE t",
        "EXCHANGE TABLES a AND b",
    ],
)
def test_clickhouse_rejects_its_own_hazards(sql):
    with pytest.raises(UnsafeSQLError):
        validate_sql(sql, CLICKHOUSE_DIALECT)


def test_a_dollar_quoted_body_is_inert_like_any_string_literal():
    """The point of scanning them is to avoid false positives.

    A keyword or semicolon *inside* the body is data, exactly as it would be
    inside '...'. Rejecting these would make legitimate Postgres queries fail
    for no reason.
    """
    assert validate_sql("SELECT $$ drop table nonsense $$ AS note", POSTGRES_DIALECT)
    assert validate_sql("SELECT $tag$ delete $tag$ AS note", POSTGRES_DIALECT)
    assert validate_sql("SELECT $$ hello ; there $$ AS note", POSTGRES_DIALECT)


def test_a_separator_outside_a_dollar_quoted_body_is_still_caught():
    """The smuggling case: close the body, then start a second statement.

    This is what dollar-quote scanning must not swallow -- getting the closing
    tag wrong would make everything after it invisible to the guard.
    """
    with pytest.raises(UnsafeSQLError):
        validate_sql("SELECT $$x$$ ; DROP TABLE t", POSTGRES_DIALECT)
    with pytest.raises(UnsafeSQLError):
        validate_sql("SELECT $tag$x$tag$ ; DELETE FROM t", POSTGRES_DIALECT)


def test_a_nested_looking_tag_closes_on_its_own_tag_only():
    """$tag$ is not closed by a bare $$ inside it, so the DROP stays outside."""
    with pytest.raises(UnsafeSQLError):
        validate_sql("SELECT $tag$ a $$ b $tag$; DROP TABLE t", POSTGRES_DIALECT)


def test_clickhouse_does_not_treat_dollars_as_quoting():
    """ClickHouse has no dollar quoting, so a $ must not hide a keyword."""
    with pytest.raises(UnsafeSQLError):
        validate_sql("SELECT $$ DROP TABLE t $$", CLICKHOUSE_DIALECT)


def test_double_quoted_identifiers_are_opaque_on_both_engines():
    """Postgres quotes identifiers with ", and a keyword inside one is inert."""
    for dialect in (POSTGRES_DIALECT, CLICKHOUSE_DIALECT):
        assert validate_sql('SELECT "drop" FROM "public"."t"', dialect)
        # A semicolon inside an identifier is not a statement separator.
        assert validate_sql('SELECT 1 AS "weird;name"', dialect)


def test_clickhouse_functions_are_not_rejected_as_keywords():
    assert validate_sql(
        "SELECT toStartOfMonth(ts), uniqExact(id) FROM db.t GROUP BY 1",
        CLICKHOUSE_DIALECT,
    )


def test_postgres_reads_that_look_alarming_are_allowed():
    # date_trunc and a CTE, plus a column literally called "insert_count".
    assert validate_sql(
        "WITH m AS (SELECT date_trunc('month', ts) AS b FROM s.events) "
        'SELECT b, count(*) AS "insert_count" FROM m GROUP BY b',
        POSTGRES_DIALECT,
    )


def test_ensure_limit_is_dialect_aware_but_agrees():
    for dialect in (POSTGRES_DIALECT, CLICKHOUSE_DIALECT):
        assert "LIMIT 100" in ensure_limit("SELECT * FROM t", 100, dialect)
        assert "LIMIT" not in ensure_limit("EXPLAIN SELECT 1", 100, dialect)

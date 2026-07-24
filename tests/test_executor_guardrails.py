"""Guardrail tests for the read-only SQL validator (no live DB required)."""

import pytest

from datatalk.agent.executor import (
    UnsafeSQLError,
    ensure_limit,
    validate_sql,
)


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

"""Safe, read-only SQL execution.

The LLM writes SQL, so every statement passes through :func:`validate_sql`
before it can run. Validation is done with a small tokenizer that is aware of
string literals (``'...'`` with ``''`` escaping) and backtick-quoted
identifiers, so keywords hidden inside strings/identifiers or after comments do
not fool the guard and do not trigger false positives.

Defense-in-depth (see README/.env.example): also point ``CLICKHOUSE_USER`` at a
read-only ClickHouse user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from clickhouse_connect.driver.client import Client

from datatalk.config import Settings, get_settings
from datatalk.db.clickhouse import get_client

# Statements the agent is allowed to run (must be the first keyword).
_ALLOWED_LEADERS = {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}

# Keywords that must never appear at the top level of a statement.
_FORBIDDEN = {
    "INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "CREATE", "TRUNCATE",
    "RENAME", "ATTACH", "DETACH", "OPTIMIZE", "GRANT", "REVOKE", "SET",
    "SYSTEM", "KILL", "USE", "CALL", "REPLACE", "MOVE", "EXCHANGE", "UNDROP",
}

# Statements for which we do NOT auto-inject a LIMIT.
_NO_LIMIT_LEADERS = {"SHOW", "DESCRIBE", "DESC", "EXPLAIN"}

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class UnsafeSQLError(ValueError):
    """Raised when a statement fails the read-only guardrails."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    row_count: int
    truncated: bool
    sql: str

    def to_records(self) -> list[dict]:
        return [dict(zip(self.columns, row)) for row in self.rows]


def _strip_and_scan(sql: str) -> tuple[str, list[str]]:
    """Return (sql-without-comments, top-level UPPERCASE word tokens).

    "Top level" means not inside a string literal or backtick identifier.
    Statement-separator semicolons that are not trailing raise UnsafeSQLError.
    """
    out: list[str] = []
    words: list[str] = []
    i = 0
    n = len(sql)
    seen_nonspace_after_semicolon = False

    while i < n:
        ch = sql[i]

        # String literal
        if ch == "'":
            out.append(ch)
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":  # escaped quote
                        out.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # Backtick-quoted identifier
        if ch == "`":
            out.append(ch)
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == "`":
                    i += 1
                    break
                i += 1
            continue

        # Line comment
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                i += 1
            out.append(" ")
            continue

        # Block comment
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")
            continue

        # Statement separator
        if ch == ";":
            # allow only a single trailing statement terminator
            rest = sql[i + 1 :].strip()
            if rest and not rest.startswith("--"):
                raise UnsafeSQLError("Multiple SQL statements are not allowed.")
            i += 1
            continue

        # Word
        m = _WORD_RE.match(sql, i)
        if m:
            word = m.group(0)
            words.append(word.upper())
            out.append(word)
            i = m.end()
            continue

        out.append(ch)
        i += 1

    return "".join(out), words


def validate_sql(sql: str) -> str:
    """Validate that ``sql`` is a single read-only statement.

    Returns the comment-stripped SQL on success; raises UnsafeSQLError otherwise.
    """
    sql = (sql or "").strip()
    if not sql:
        raise UnsafeSQLError("Empty SQL statement.")

    cleaned, words = _strip_and_scan(sql)
    if not words:
        raise UnsafeSQLError("No SQL keywords found.")

    leader = words[0]
    if leader not in _ALLOWED_LEADERS:
        raise UnsafeSQLError(
            f"Statement must start with one of {sorted(_ALLOWED_LEADERS)}, got '{leader}'."
        )

    forbidden_hits = sorted({w for w in words if w in _FORBIDDEN})
    if forbidden_hits:
        raise UnsafeSQLError(
            f"Forbidden keyword(s) present: {', '.join(forbidden_hits)}."
        )

    return cleaned.strip()


def _has_top_level_limit(words: list[str]) -> bool:
    return "LIMIT" in words


def ensure_limit(sql: str, default_limit: int) -> str:
    """Append a LIMIT to exploratory SELECT/WITH queries that lack one."""
    _, words = _strip_and_scan(sql)
    if not words:
        return sql
    if words[0] in _NO_LIMIT_LEADERS:
        return sql
    if _has_top_level_limit(words):
        return sql
    return f"{sql.rstrip().rstrip(';')}\nLIMIT {int(default_limit)}"


def run_sql(
    sql: str,
    client: Client | None = None,
    settings: Settings | None = None,
) -> QueryResult:
    """Validate, cap, and execute a read-only query.

    Raises :class:`UnsafeSQLError` if the statement is not read-only.
    """
    settings = settings or get_settings()
    client = client or get_client()

    safe_sql = validate_sql(sql)
    safe_sql = ensure_limit(safe_sql, settings.sql_default_limit)

    query_settings = {
        "max_execution_time": settings.sql_timeout_seconds,
        # server-side cap as a second layer over our own row slicing
        "max_result_rows": settings.sql_max_rows,
        "result_overflow_mode": "break",
    }
    result = client.query(safe_sql, settings=query_settings)

    all_rows = [list(r) for r in result.result_rows]
    truncated = len(all_rows) > settings.sql_max_rows
    rows = all_rows[: settings.sql_max_rows]

    columns = list(result.column_names)

    return QueryResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
        sql=safe_sql,
    )

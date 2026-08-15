"""Safe, read-only SQL execution.

The LLM writes SQL, so every statement passes through :func:`validate_sql`
before it can run. Validation is done with a small tokenizer that is aware of
string literals (``'...'`` with ``''`` escaping), quoted identifiers, and --
where the engine has them -- dollar-quoted bodies, so keywords hidden inside
strings/identifiers or after comments do not fool the guard and do not trigger
false positives.

The rules are per-:class:`~datatalk.warehouse.base.Dialect`: the allowed leaders
are shared, but each engine adds its own unsafe verbs (ClickHouse ``SYSTEM``,
Postgres ``COPY``) and its own quoting.

Defense-in-depth: on Postgres every session is opened ``read_only``, so a
statement that slips past this parser still cannot write. ClickHouse has no
equivalent, so point ``CLICKHOUSE_USER`` at a read-only user (see README).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from datatalk.warehouse.base import (
    ALLOWED_LEADERS,
    NO_LIMIT_LEADERS,
    Dialect,
    QueryResult,
)
from datatalk.warehouse.clickhouse import CLICKHOUSE_DIALECT

if TYPE_CHECKING:  # avoid a circular import: context -> clients -> warehouse
    from datatalk.context import TenantContext

# Re-exported: QueryResult used to be defined here and is imported from this
# module across the agent package.
__all__ = ["QueryResult", "UnsafeSQLError", "validate_sql", "ensure_limit", "run_sql"]

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


class UnsafeSQLError(ValueError):
    """Raised when a statement fails the read-only guardrails."""


def _strip_and_scan(sql: str, dialect: Dialect) -> tuple[str, list[str]]:
    """Return (sql-without-comments, top-level UPPERCASE word tokens).

    "Top level" means not inside a string literal, a quoted identifier, or a
    dollar-quoted body. Statement-separator semicolons that are not trailing
    raise UnsafeSQLError.
    """
    out: list[str] = []
    words: list[str] = []
    i = 0
    n = len(sql)

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

        # Quoted identifier. Both engines accept double quotes; ClickHouse also
        # accepts backticks. Scanning either is safe on either engine -- the
        # cost of missing one is a keyword inside an identifier tripping the
        # guard, the cost of missing the other is far worse.
        if ch in ('"', "`"):
            closer = ch
            out.append(ch)
            i += 1
            while i < n:
                out.append(sql[i])
                if sql[i] == closer:
                    i += 1
                    break
                i += 1
            continue

        # Dollar-quoted body ($$...$$ / $tag$...$tag$). Postgres only: elsewhere
        # a bare $ is an ordinary character and treating it as a quote would let
        # a keyword hide behind it.
        if ch == "$" and dialect.supports_dollar_quoting:
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, m.end())
                # An unterminated body swallows the rest of the statement,
                # which is exactly what the server would do too.
                stop = n if end == -1 else end + len(tag)
                out.append(" ")
                i = stop
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


def validate_sql(sql: str, dialect: Dialect = CLICKHOUSE_DIALECT) -> str:
    """Validate that ``sql`` is a single read-only statement for ``dialect``.

    Returns the comment-stripped SQL on success; raises UnsafeSQLError otherwise.
    """
    sql = (sql or "").strip()
    if not sql:
        raise UnsafeSQLError("Empty SQL statement.")

    cleaned, words = _strip_and_scan(sql, dialect)
    if not words:
        raise UnsafeSQLError("No SQL keywords found.")

    leader = words[0]
    if leader not in ALLOWED_LEADERS:
        raise UnsafeSQLError(
            f"Statement must start with one of {sorted(ALLOWED_LEADERS)}, got '{leader}'."
        )

    forbidden = dialect.forbidden
    forbidden_hits = sorted({w for w in words if w in forbidden})
    if forbidden_hits:
        raise UnsafeSQLError(
            f"Forbidden keyword(s) present: {', '.join(forbidden_hits)}."
        )

    return cleaned.strip()


def _has_top_level_limit(words: list[str]) -> bool:
    return "LIMIT" in words


def ensure_limit(
    sql: str, default_limit: int, dialect: Dialect = CLICKHOUSE_DIALECT
) -> str:
    """Append a LIMIT to exploratory SELECT/WITH queries that lack one."""
    _, words = _strip_and_scan(sql, dialect)
    if not words:
        return sql
    if words[0] in NO_LIMIT_LEADERS:
        return sql
    if _has_top_level_limit(words):
        return sql
    return f"{sql.rstrip().rstrip(';')}\nLIMIT {int(default_limit)}"


def run_sql(
    sql: str,
    *,
    ctx: "TenantContext",
    source: str | None = None,
    parameters: Mapping[str, Any] | None = None,
) -> QueryResult:
    """Validate, cap, and execute a read-only query against one of the org's sources.

    ``source`` names a source configured for the org; ``None`` uses the default
    one. Raises :class:`UnsafeSQLError` if the statement is not read-only for
    that source's engine, :class:`~datatalk.context.UnknownSourceError` if the
    name is not configured, and
    :class:`~datatalk.context.NoConnectionError` if the org has no sources.

    ``parameters`` are bound by the driver, never interpolated. Dashboard filter
    values arrive this way, which is why validation below still sees a statement
    that does not vary with user input: the guardrails check the same bytes on
    every refresh, and the value is never among them.
    """
    ref = ctx.source(source)
    warehouse = ctx.warehouse(source)

    safe_sql = validate_sql(sql, warehouse.dialect)
    safe_sql = ensure_limit(safe_sql, ref.spec.sql_default_limit, warehouse.dialect)

    return warehouse.query(
        safe_sql,
        timeout_s=ref.spec.sql_timeout_seconds,
        max_rows=ref.spec.sql_max_rows,
        parameters=parameters,
    )

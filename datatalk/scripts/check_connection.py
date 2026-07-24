"""Connection checkout (Milestone 1).

Run this first, against your real ClickHouse:

    python -m datatalk.scripts.check_connection
    # or, after `pip install -e .`
    datatalk-check

It verifies:
  1. ClickHouse connectivity + server version.
  2. Schema discovery (databases, tables, columns, sample rows).
  3. OpenAI API key + model with one tiny test call.

Nothing is written anywhere; this is a read-only smoke test.
"""

from __future__ import annotations

import sys

from datatalk.config import get_settings


def _print_header(title: str) -> None:
    print(f"\n=== {title} ===")


def check_clickhouse() -> bool:
    from datatalk.db import clickhouse, introspect

    _print_header("ClickHouse")
    settings = get_settings()
    print(
        f"Connecting to {settings.clickhouse_host}:{settings.clickhouse_port} "
        f"(db={settings.clickhouse_database}, secure={settings.clickhouse_secure})"
    )
    try:
        info = clickhouse.ping()
    except Exception as exc:  # noqa: BLE001 - surface any driver error
        print(f"  FAILED: {exc}")
        return False

    print(f"  OK — server version {info['version']}, current db {info['database']}")

    _print_header("Schema discovery")
    try:
        tables = introspect.introspect(with_samples=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED to introspect: {exc}")
        return False

    if not tables:
        print("  WARNING: no user tables found (only system databases).")
        return True

    print(f"  Found {len(tables)} table(s):")
    print(introspect.schema_summary(tables))

    # Show a fuller preview of the first table so the user can eyeball it.
    first = tables[0]
    _print_header(f"Sample: {first.qualified_name}")
    for col in first.columns[:20]:
        comment = f"  -- {col.comment}" if col.comment else ""
        print(f"  {col.name}: {col.type}{comment}")
    if first.sample_rows:
        print("  sample rows:")
        for row in first.sample_rows:
            print(f"    {row}")
    return True


def check_openai() -> bool:
    _print_header("OpenAI")
    settings = get_settings()
    if not settings.has_openai:
        print("  SKIPPED: OPENAI_API_KEY not set (copy .env.example to .env).")
        return False
    try:
        from datatalk.llm import client

        reply = client.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {exc}")
        return False
    print(f"  OK — model {settings.openai_model} replied: {reply!r}")
    return True


def main() -> int:
    print("DataTalk connection checkout")
    ch_ok = check_clickhouse()
    oa_ok = check_openai()

    _print_header("Summary")
    print(f"  ClickHouse: {'OK' if ch_ok else 'FAILED'}")
    print(f"  OpenAI:     {'OK' if oa_ok else 'FAILED/SKIPPED'}")

    return 0 if (ch_ok and oa_ok) else 1


if __name__ == "__main__":
    sys.exit(main())

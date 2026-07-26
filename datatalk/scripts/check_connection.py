"""Connection checkout (Milestone 1).

Run this first, against the deployment's own warehouse:

    python -m datatalk.scripts.check_connection
    # or, after `pip install -e .`
    datatalk-check

It verifies:
  1. Warehouse connectivity + server version, for every source in the context.
  2. Schema discovery (namespaces, tables, columns, sample rows).
  3. OpenAI API key + model with one tiny test call.

Nothing is written anywhere; this is a read-only smoke test. It checks the
environment's ``CLICKHOUSE_*`` credentials, which are the deployment's own --
an org's sources live in Postgres and are checked from the settings UI.
"""

from __future__ import annotations

import sys

from datatalk.context import SourceRef, TenantContext
from datatalk.warehouse import catalog


def _print_header(title: str) -> None:
    print(f"\n=== {title} ===")


def check_source(ctx: TenantContext, ref: SourceRef) -> bool:
    _print_header(f"Source {ref.name} [{ref.type}]")
    spec = ref.spec
    print(
        f"Connecting to {spec.host}:{spec.port} "
        f"(db={spec.database}, secure={spec.secure})"
    )
    try:
        warehouse = ctx.warehouse(ref.name)
        info = warehouse.ping()
    except Exception as exc:  # noqa: BLE001 - surface any driver error
        print(f"  FAILED: {exc}")
        return False

    print(f"  OK — server version {info['version']}, current db {info['database']}")

    _print_header(f"Schema discovery ({ref.name})")
    try:
        tables = warehouse.introspect(with_samples=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED to introspect: {exc}")
        return False

    if not tables:
        print("  WARNING: no user tables found (only system namespaces).")
        return True

    print(f"  Found {len(tables)} table(s):")
    print(catalog.schema_summary(tables))

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


def check_openai(ctx: TenantContext) -> bool:
    _print_header("OpenAI")
    settings = ctx.settings
    if not settings.has_openai:
        print("  SKIPPED: OPENAI_API_KEY not set (copy .env.example to .env).")
        return False
    try:
        from datatalk.llm import client

        reply = client.ping(ctx)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {exc}")
        return False
    print(f"  OK — model {settings.openai_model} replied: {reply!r}")
    return True


def main() -> int:
    print("DataTalk connection checkout")
    # A single-tenant context straight from .env: this script predates orgs and
    # checks the environment's own credentials.
    ctx = TenantContext.from_env()
    results = {ref.name: check_source(ctx, ref) for ref in ctx.sources}
    oa_ok = check_openai(ctx)

    _print_header("Summary")
    for name, ok in results.items():
        print(f"  Source {name}: {'OK' if ok else 'FAILED'}")
    print(f"  OpenAI:     {'OK' if oa_ok else 'FAILED/SKIPPED'}")

    return 0 if (all(results.values()) and oa_ok) else 1


if __name__ == "__main__":
    sys.exit(main())

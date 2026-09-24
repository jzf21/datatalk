"""Refresh synced sources (Jira). Meant for cron.

    datatalk-sync                          # every synced source, incrementally
    datatalk-sync --org acme --source jira # one source
    datatalk-sync --full                   # re-read everything; notices deletions
    datatalk-sync --gc --dry-run           # list sync-store schemas no source owns
    datatalk-sync --gc                     # ...and drop them

There is no in-process scheduler on purpose: every uvicorn worker would run it.
Runs are serialized per source by an advisory lock in the sync store, so an
overlapping cron run, or a click on "Sync now", is skipped rather than doubled.

Exit status is 1 if any source failed, so cron's mail tells you.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from datatalk.db import models
from datatalk.db import session as db_session_mod
from datatalk.integrations import syncstore
from datatalk.integrations.jira import service


def _targets(org_slug: str | None, source: str | None) -> list[tuple]:
    with db_session_mod.session_scope() as db:
        stmt = (
            select(models.OrgWarehouseConnection, models.Org.slug)
            .join(models.Org, models.Org.id == models.OrgWarehouseConnection.org_id)
            .where(models.OrgWarehouseConnection.type == "jira")
            .order_by(models.Org.slug, models.OrgWarehouseConnection.name)
        )
        if org_slug:
            stmt = stmt.where(models.Org.slug == org_slug)
        if source:
            stmt = stmt.where(models.OrgWarehouseConnection.name == source)
        return [
            (c.org_id, c.id, slug, c.name)
            for c, slug in db.execute(stmt).all()
            if c.sync_state is not None
        ]


def _gc(dry_run: bool) -> int:
    with db_session_mod.session_scope() as db:
        owned = set(db.scalars(select(models.SourceSyncState.schema_name)))
    with syncstore.admin_connection() as conn:
        orphans = [s for s in syncstore.managed_schemas(conn) if s not in owned]
        for schema in orphans:
            role = syncstore.role_for_schema(schema)
            print(f"{'would drop' if dry_run else 'dropping'} {schema} / {role}")
            if not dry_run:
                syncstore.drop(conn, schema, role)
    if not orphans:
        print("no orphaned schemas")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="datatalk-sync",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--org", help="org slug")
    parser.add_argument("--source", help="source name within the org")
    parser.add_argument("--full", action="store_true", help="full resync")
    parser.add_argument("--gc", action="store_true", help="drop orphaned schemas")
    parser.add_argument("--dry-run", action="store_true", help="with --gc: only list")
    args = parser.parse_args(argv)

    if not syncstore.is_configured():
        print(syncstore.SyncStoreNotConfiguredError(), file=sys.stderr)
        return 2
    if args.gc:
        return _gc(args.dry_run)

    targets = _targets(args.org, args.source)
    if not targets:
        print("no synced sources matched")
        return 0

    failed = 0
    for org_id, conn_id, slug, name in targets:
        label = f"{slug}/{name}"
        try:
            state = service.sync_connection(org_id, conn_id, full=args.full)
        except syncstore.SyncInProgressError:
            print(f"{label}: already syncing, skipped")
            continue
        except Exception as exc:  # noqa: BLE001 - report and move to the next source
            failed += 1
            print(f"{label}: FAILED: {exc}", file=sys.stderr)
            continue
        st = state.get("stats") or {}
        print(
            f"{label}: ok ({'full' if st.get('full') else 'incremental'}, "
            f"{st.get('issues_synced', 0)} issues in {st.get('seconds', 0)}s)"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

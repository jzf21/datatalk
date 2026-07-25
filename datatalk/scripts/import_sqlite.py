"""One-shot import of the legacy single-tenant SQLite store into Postgres.

    datatalk-import-sqlite --sqlite datatalk.sqlite3 \\
        --org-name Acme --owner-email you@example.com --owner-password '...' \\
        --bootstrap-clickhouse

The old store had no notion of an org, so every row it holds belongs to exactly
one tenant: the one named on the command line. This finds-or-creates that org
and its owner, then copies suggestions, reports, qa_turns and dashboards into
it.

Three properties worth knowing:

* **Ids are remapped, not preserved.** The Postgres content tables share one
  global ``BIGSERIAL``; keeping the old ids would need ``setval`` and would
  collide the moment a *second* legacy file is imported. ``qa_turns.report_id``
  is rewritten through an in-memory ``{legacy_id: new_id}`` map.
* **Re-running is a no-op.** Every imported row carries ``legacy_id``, and the
  partial unique index on ``(org_id, legacy_id)`` backs that up in the database.
* **The source file is opened read-only** (``mode=ro``), so a botched run cannot
  damage it. Keep the file until you have verified the import.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from datatalk.auth import passwords
from datatalk.auth.orgs import slugify
from datatalk.config import get_settings
from datatalk.db import models
from datatalk.db.session import get_engine
from datatalk.scripts.db import alembic_config, head_revision, schema_is_current

_TABLES = ("suggestions", "reports", "qa_turns", "dashboards")


@dataclass
class Counts:
    imported: int = 0
    skipped: int = 0  # already present from an earlier run
    orphaned: int = 0  # qa_turns whose report is missing


@dataclass
class Summary:
    org_slug: str = ""
    owner_email: str = ""
    created_org: bool = False
    created_user: bool = False
    created_connection: bool = False
    tables: dict[str, Counts] = field(default_factory=dict)

    def render(self) -> str:
        lines = [
            f"org:   {self.org_slug}" + (" (created)" if self.created_org else ""),
            f"owner: {self.owner_email}" + (" (created)" if self.created_user else ""),
        ]
        if self.created_connection:
            lines.append("clickhouse connection: created from the environment")
        for name in _TABLES:
            c = self.tables.get(name)
            if c is None:
                continue
            line = f"{name + ':':<13}{c.imported} imported, {c.skipped} already present"
            if c.orphaned:
                line += f", {c.orphaned} orphaned (skipped)"
            lines.append(line)
        return "\n".join(lines)


# --- reading the legacy file --------------------------------------------------


def open_legacy(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"No such SQLite file: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def _rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    """Every row of ``table``, oldest first, or nothing if it predates the table."""
    if not _table_exists(conn, table):
        return []
    return list(conn.execute(f"SELECT * FROM {table} ORDER BY id"))  # noqa: S608


def _get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """Column access tolerant of schema drift across old files."""
    return row[key] if key in row.keys() else default


def _ts(value: Any) -> datetime:
    """``created_at TEXT`` -> aware ``TIMESTAMPTZ``. Naive values are read as UTC."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _json(value: Any, fallback: Any) -> Any:
    """Legacy JSON text -> Python. NULL and unparseable both fall back."""
    if value in (None, ""):
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


# --- identity -----------------------------------------------------------------


def find_or_create_org(db: Session, *, name: str, slug: str) -> tuple[models.Org, bool]:
    existing = db.execute(
        select(models.Org).where(models.Org.slug == slug)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    org = models.Org(name=name, slug=slug)
    db.add(org)
    db.flush()
    return org, True


def find_or_create_owner(
    db: Session, *, email: str, password: str | None
) -> tuple[models.User, bool]:
    normalized = email.strip().lower()
    existing = db.execute(
        select(models.User).where(models.User.email == normalized)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    if not password:
        raise SystemExit(
            f"No user {normalized} exists yet, so --owner-password is required "
            "to create one."
        )
    try:
        password_hash = passwords.hash_password(password)
    except passwords.PasswordPolicyError as exc:
        raise SystemExit(str(exc)) from exc
    user = models.User(email=normalized, password_hash=password_hash)
    db.add(user)
    db.flush()
    return user, True


def ensure_membership(db: Session, *, org_id: UUID, user_id: UUID) -> None:
    existing = db.execute(
        select(models.Membership).where(
            models.Membership.org_id == org_id,
            models.Membership.user_id == user_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(models.Membership(org_id=org_id, user_id=user_id, role="owner"))
        db.flush()


def bootstrap_connection(db: Session, *, org_id: UUID, user_id: UUID) -> bool:
    """Create the org's ClickHouse connection from the current environment.

    The password is encrypted by the column type on the way in; there is no path
    through this function that stores it in plaintext.
    """
    if db.execute(
        select(models.OrgClickHouseConnection.id).where(
            models.OrgClickHouseConnection.org_id == org_id
        )
    ).first():
        return False

    s = get_settings()
    db.add(
        models.OrgClickHouseConnection(
            org_id=org_id,
            name="default",
            is_default=True,
            host=s.clickhouse_host,
            port=s.clickhouse_port,
            username=s.clickhouse_user,
            password=s.clickhouse_password or None,
            database=s.clickhouse_database,
            secure=s.clickhouse_secure,
            introspect_databases=list(s.introspect_database_list),
            introspect_exclude_patterns=list(s.introspect_exclude_list),
            updated_by_user_id=user_id,
        )
    )
    db.flush()
    return True


# --- content ------------------------------------------------------------------


def _already_imported(db: Session, model, org_id: UUID) -> dict[int, int]:
    """``{legacy_id: new_id}`` for rows this org already has."""
    rows = db.execute(
        select(model.legacy_id, model.id).where(
            model.org_id == org_id, model.legacy_id.is_not(None)
        )
    ).all()
    return {int(legacy): int(new) for legacy, new in rows}


def import_suggestions(
    db: Session, legacy: sqlite3.Connection, *, org_id: UUID, user_id: UUID
) -> Counts:
    counts = Counts()
    seen = _already_imported(db, models.Suggestion, org_id)
    embed_model = get_settings().openai_embed_model
    for row in _rows(legacy, "suggestions"):
        if row["id"] in seen:
            counts.skipped += 1
            continue
        blob = bytes(row["embedding"] or b"")
        db.add(
            models.Suggestion(
                org_id=org_id,
                created_by_user_id=user_id,
                text_=row["text"],
                embedding=blob,
                # The old schema recorded neither; float32 was the only format
                # ever written, and the model can only be the configured one.
                embedding_dim=len(blob) // 4,
                embedding_model=embed_model,
                legacy_id=row["id"],
                created_at=_ts(row["created_at"]),
            )
        )
        counts.imported += 1
    db.flush()
    return counts


def import_reports(
    db: Session, legacy: sqlite3.Connection, *, org_id: UUID, user_id: UUID
) -> tuple[Counts, dict[int, int]]:
    """Copy reports and return the ``{legacy_id: new_id}`` map qa_turns needs.

    The map is seeded from rows already present so a second run still remaps
    qa_turns correctly instead of declaring them all orphans.
    """
    counts = Counts()
    id_map = _already_imported(db, models.Report, org_id)
    counts.skipped = len(id_map)

    fresh: list[tuple[int, models.Report]] = []
    for row in _rows(legacy, "reports"):
        if row["id"] in id_map:
            continue
        report = models.Report(
            org_id=org_id,
            created_by_user_id=user_id,
            request=row["request"],
            markdown=row["markdown"],
            # These two columns were added by a later migration, so early rows
            # have NULL where the new schema requires an object.
            document=_json(_get(row, "document"), {}),
            queries=_json(_get(row, "queries"), []),
            legacy_id=row["id"],
            created_at=_ts(row["created_at"]),
        )
        db.add(report)
        fresh.append((row["id"], report))

    db.flush()  # populates report.id
    for legacy_id, report in fresh:
        id_map[legacy_id] = report.id
        counts.imported += 1
    return counts, id_map


def import_qa_turns(
    db: Session,
    legacy: sqlite3.Connection,
    *,
    org_id: UUID,
    user_id: UUID,
    report_ids: dict[int, int],
) -> Counts:
    counts = Counts()
    seen = _already_imported(db, models.QATurn, org_id)
    for row in _rows(legacy, "qa_turns"):
        if row["id"] in seen:
            counts.skipped += 1
            continue
        new_report_id = report_ids.get(row["report_id"])
        if new_report_id is None:
            # The old schema had no foreign key, so dangling report_ids are
            # possible. The composite FK would reject them; skip and report.
            counts.orphaned += 1
            continue
        db.add(
            models.QATurn(
                org_id=org_id,
                report_id=new_report_id,
                created_by_user_id=user_id,
                question=row["question"],
                answer_document=_json(row["answer_document"], {}),
                queries=_json(row["queries"], []),
                legacy_id=row["id"],
                created_at=_ts(row["created_at"]),
            )
        )
        counts.imported += 1
    db.flush()
    return counts


def import_dashboards(
    db: Session, legacy: sqlite3.Connection, *, org_id: UUID, user_id: UUID
) -> Counts:
    counts = Counts()
    seen = _already_imported(db, models.Dashboard, org_id)
    for row in _rows(legacy, "dashboards"):
        if row["id"] in seen:
            counts.skipped += 1
            continue
        db.add(
            models.Dashboard(
                org_id=org_id,
                created_by_user_id=user_id,
                request=row["request"],
                title=row["title"],
                document=_json(row["document"], {}),
                queries=_json(row["queries"], []),
                analysis=_get(row, "analysis"),
                legacy_id=row["id"],
                created_at=_ts(row["created_at"]),
            )
        )
        counts.imported += 1
    db.flush()
    return counts


def run_import(
    db: Session,
    legacy: sqlite3.Connection,
    *,
    org_name: str,
    org_slug: str,
    owner_email: str,
    owner_password: str | None,
    bootstrap_clickhouse: bool = False,
) -> Summary:
    """Import everything into one transaction. The caller commits or rolls back."""
    summary = Summary(org_slug=org_slug, owner_email=owner_email.strip().lower())

    org, summary.created_org = find_or_create_org(db, name=org_name, slug=org_slug)
    user, summary.created_user = find_or_create_owner(
        db, email=owner_email, password=owner_password
    )
    ensure_membership(db, org_id=org.id, user_id=user.id)
    if bootstrap_clickhouse:
        summary.created_connection = bootstrap_connection(
            db, org_id=org.id, user_id=user.id
        )

    scope = {"org_id": org.id, "user_id": user.id}
    summary.tables["suggestions"] = import_suggestions(db, legacy, **scope)
    reports, report_ids = import_reports(db, legacy, **scope)
    summary.tables["reports"] = reports
    summary.tables["qa_turns"] = import_qa_turns(
        db, legacy, report_ids=report_ids, **scope
    )
    summary.tables["dashboards"] = import_dashboards(db, legacy, **scope)
    return summary


# --- cli ----------------------------------------------------------------------


def _require_current_schema(engine, url: str | None) -> None:
    cfg = alembic_config(url)
    if not schema_is_current(engine, cfg):
        raise SystemExit(
            "The database schema is not at the newest revision "
            f"({head_revision(cfg)}). Run `datatalk-db upgrade` first."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="datatalk-import-sqlite",
        description="Import a legacy single-tenant datatalk.sqlite3 into an org.",
    )
    parser.add_argument("--sqlite", default="datatalk.sqlite3", type=Path)
    parser.add_argument("--org-name", required=True)
    parser.add_argument("--org-slug", help="defaults to a slug of --org-name")
    parser.add_argument("--owner-email", required=True)
    parser.add_argument(
        "--owner-password", help="required only when the user does not exist yet"
    )
    parser.add_argument(
        "--bootstrap-clickhouse",
        action="store_true",
        help="create the org's ClickHouse connection from the current environment",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="do the whole import, report the counts, then roll it back",
    )
    parser.add_argument("--url", help="override DATABASE_URL")
    args = parser.parse_args(argv)

    if args.url:
        from datatalk.db.session import reset_engine

        get_settings.cache_clear()
        reset_engine()

    engine = get_engine()
    _require_current_schema(engine, args.url)

    if args.bootstrap_clickhouse:
        # Fail before touching anything rather than at flush time, when the
        # error surfaces as an opaque encryption failure mid-import.
        from datatalk.security import crypto

        crypto.get_fernet()

    legacy = open_legacy(args.sqlite)
    session = Session(bind=engine, expire_on_commit=False)
    try:
        summary = run_import(
            session,
            legacy,
            org_name=args.org_name,
            org_slug=args.org_slug or slugify(args.org_name),
            owner_email=args.owner_email,
            owner_password=args.owner_password,
            bootstrap_clickhouse=args.bootstrap_clickhouse,
        )
        if args.dry_run:
            session.rollback()
        else:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        legacy.close()

    print(summary.render())
    if args.dry_run:
        print("\n--dry-run: everything above was rolled back.")
    else:
        print(f"\nDone. Sign in as {summary.owner_email} to see the imported data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

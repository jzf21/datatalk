"""The migrations and the ORM models must describe the same schema.

Both revisions are hand-written -- Alembic's autogenerate does not emit the
partial unique indexes, functional indexes, and composite foreign keys that
enforce tenant isolation here. Hand-written means drift is possible, and drift
in exactly those constraints is the failure mode that matters.

This runs autogenerate *against the migrated database* and asserts it finds
nothing left to do.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from datatalk.db.models import Base

# Autogenerate reports differences it cannot express as well as real drift.
# Index and constraint *ordering* and server-default reformatting are noise;
# a missing table or column is not.
_MEANINGFUL = {
    "add_table",
    "remove_table",
    "add_column",
    "remove_column",
    "modify_nullable",
    "modify_type",
}


def test_models_and_migrations_agree(engine):
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        diff = compare_metadata(context, Base.metadata)

    meaningful = [d for d in diff if _describe(d) in _MEANINGFUL]
    assert not meaningful, f"models and migrations have drifted: {meaningful}"


def _describe(diff) -> str:
    # Entries are either a tuple whose first element names the operation, or a
    # list of such tuples for column-level changes.
    if isinstance(diff, list):
        return diff[0][0] if diff and isinstance(diff[0], tuple) else ""
    return diff[0] if isinstance(diff, tuple) else ""


# --- what revision 0002 was for -----------------------------------------------


def test_the_connection_table_was_renamed(engine):
    names = set(inspect(engine).get_table_names())
    assert "org_warehouse_connections" in names
    assert "org_clickhouse_connections" not in names


def test_the_renamed_constraints_came_with_it(engine):
    """A rename leaves constraint names behind unless they are renamed too."""
    inspector = inspect(engine)
    table = "org_warehouse_connections"

    unique = {c["name"] for c in inspector.get_unique_constraints(table)}
    indexes = {i["name"] for i in inspector.get_indexes(table)}

    assert "ux_whconn_org_name" in unique
    assert "ux_whconn_org_default" in indexes
    assert not any(n and n.startswith("ux_chconn") for n in unique | indexes)


def test_the_engine_columns_exist_with_their_defaults(engine):
    columns = {
        c["name"]: c
        for c in inspect(engine).get_columns("org_warehouse_connections")
    }

    assert not columns["type"]["nullable"]
    # Existing rows predate the column, so the default has to say ClickHouse.
    assert "clickhouse" in str(columns["type"]["default"])
    assert not columns["description"]["nullable"]
    assert columns["sslmode"]["nullable"]


@pytest.mark.parametrize("type_", ["clickhouse", "postgres"])
def test_the_type_check_accepts_both_engines(db, org_a, type_):
    from datatalk.db import models

    conn = models.OrgWarehouseConnection(
        org_id=org_a.id, name=f"s_{type_}", type=type_, host="h", is_default=False
    )
    db.add(conn)
    db.flush()  # the CHECK fires here
    assert conn.type == type_


def test_the_type_check_rejects_an_unknown_engine(db, org_a):
    from sqlalchemy.exc import IntegrityError

    from datatalk.db import models

    db.add(
        models.OrgWarehouseConnection(
            org_id=org_a.id, name="bad", type="duckdb", host="h", is_default=False
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_one_default_source_per_org_is_enforced_by_the_database(db, org_a):
    """Application code demotes before promoting; this is the backstop."""
    from sqlalchemy.exc import IntegrityError

    from datatalk.db import models

    for name in ("a", "b"):
        db.add(
            models.OrgWarehouseConnection(
                org_id=org_a.id, name=name, host="h", is_default=True
            )
        )
    with pytest.raises(IntegrityError):
        db.flush()


def test_source_names_are_unique_per_org_but_not_globally(db, org_a, org_b):
    from sqlalchemy.exc import IntegrityError

    from datatalk.db import models

    # The same name in two orgs is fine -- it is the handle the agent types,
    # scoped to the workspace.
    for owner in (org_a, org_b):
        db.add(
            models.OrgWarehouseConnection(
                org_id=owner.id, name="events", host="h", is_default=False
            )
        )
    db.flush()

    db.add(
        models.OrgWarehouseConnection(
            org_id=org_a.id, name="events", host="h2", is_default=False
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()

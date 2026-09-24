"""Load the fixture into real warehouses.

Real engines, not fakes. The point of the whole package is to measure what the
agent does against a warehouse that answers like a warehouse: Postgres returns
``Decimal`` where ClickHouse returns a string, ``NULL`` sorts differently, a
``DATE`` column comes back as three different Python types depending on driver.
An in-memory stand-in would score a model against a fiction and report the
number as if it meant something.

Seeding is idempotent by being destructive: every table this module owns is
dropped and rebuilt. That is safe only because :func:`assert_safe_target` has
already refused anything that is not a ``datatalk_eval*`` database, and it is
what makes a re-seed after a fixture change a single command rather than a
migration.
"""

from __future__ import annotations

from typing import Callable, Iterable

from datatalk.evals import dataset
from datatalk.evals.dataset import COLUMNS, COMMENTS, SOURCE_TABLES, Fixture
from datatalk.evals.targets import (
    EvalTargets,
    assert_safe_target,
    postgres_dsn,
)
from datatalk.warehouse import WarehouseSpec

Progress = Callable[[str], None]


def _noop(message: str) -> None:  # pragma: no cover - default sink
    pass


# --- Postgres -----------------------------------------------------------------


def ensure_database(admin_dsn: str, database: str, *, log: Progress = _noop) -> bool:
    """Create ``database`` if it is absent. Returns True if it was created.

    Done here rather than in ``scripts/init-test-db.sql`` because that script
    only runs on a *fresh* Postgres volume, and most developers seeding evals
    already have one. A setup step that silently no-ops for everyone who has run
    the project before is worse than no setup step.
    """
    import psycopg

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
        if exists:
            return False
        # Identifier, not a value, so it cannot be bound -- hence the guard in
        # assert_safe_target on the name's shape.
        conn.execute(f'CREATE DATABASE "{database}"')
        log(f"created database {database}")
        return True


def seed_postgres(
    spec: WarehouseSpec,
    fixture: Fixture,
    tables: Iterable[str],
    *,
    log: Progress = _noop,
) -> dict[str, int]:
    """Drop, create and fill one Postgres eval source."""
    import psycopg

    assert_safe_target(spec)
    counts: dict[str, int] = {}
    ordered = list(tables)

    with psycopg.connect(postgres_dsn(spec)) as conn:
        with conn.cursor() as cur:
            # Reverse order for the drop: order_items references orders
            # references customers, and CASCADE would happily take a table this
            # module does not own along with it.
            for name in reversed(ordered):
                cur.execute(f'DROP TABLE IF EXISTS "{name}"')
            for name in ordered:
                cur.execute(dataset.POSTGRES_DDL[name])
                for (table, column), comment in COMMENTS.items():
                    if table != name:
                        continue
                    # COMMENT ON takes no parameters -- it is DDL, and the
                    # server will not bind $1 there. `psycopg.sql` composes the
                    # literal with the server's own quoting rules rather than a
                    # hand-rolled escaper.
                    cur.execute(
                        psycopg.sql.SQL("COMMENT ON COLUMN {}.{} IS {}").format(
                            psycopg.sql.Identifier(name),
                            psycopg.sql.Identifier(column),
                            psycopg.sql.Literal(comment),
                        )
                    )

            for name in ordered:
                rows = getattr(fixture, name)
                columns = COLUMNS[name]
                quoted = ", ".join(f'"{c}"' for c in columns)
                # COPY rather than executemany: page_views is twelve thousand
                # rows and a re-seed is something you do while iterating on
                # cases, not once.
                with cur.copy(f'COPY "{name}" ({quoted}) FROM STDIN') as copy:
                    for row in rows:
                        copy.write_row(row)
                counts[name] = len(rows)
                log(f"  {spec.database}.{name}: {len(rows)} rows")
        conn.commit()
    return counts


# --- ClickHouse ---------------------------------------------------------------


def seed_clickhouse(
    spec: WarehouseSpec,
    fixture: Fixture,
    tables: Iterable[str],
    *,
    log: Progress = _noop,
) -> dict[str, int]:
    """Drop, create and fill the ClickHouse eval source.

    Only the ``events`` tables ever land here. Splitting the fixture across two
    *engines* is what makes the routing metric mean something beyond source
    naming: the agent has to notice that one source needs ClickHouse syntax and
    the other needs Postgres syntax, within a single question.
    """
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=spec.host,
        port=spec.port,
        username=spec.username,
        password=spec.password,
        secure=spec.secure,
        autogenerate_session_id=False,
    )
    counts: dict[str, int] = {}
    try:
        client.command(f"CREATE DATABASE IF NOT EXISTS {spec.database}")
        # No `USE`: the client is built with autogenerate_session_id=False (the
        # adapter's setting, because ClickHouse serializes concurrent queries on
        # one session), and over HTTP without a session there is nothing for
        # `USE` to change. Every statement below names the database explicitly
        # instead, which is what makes that safe.
        for name in tables:
            client.command(f"DROP TABLE IF EXISTS {spec.database}.{name}")
            ddl = dataset.CLICKHOUSE_DDL[name].replace(
                f"CREATE TABLE {name}", f"CREATE TABLE {spec.database}.{name}"
            )
            client.command(ddl)
            rows = [list(r) for r in getattr(fixture, name)]
            client.insert(
                name, rows, column_names=list(COLUMNS[name]), database=spec.database
            )
            counts[name] = len(rows)
            log(f"  {spec.database}.{name}: {len(rows)} rows")
    finally:
        client.close()
    return counts


# --- entry point --------------------------------------------------------------


def seed_all(
    targets: EvalTargets,
    *,
    events_engine: str = "postgres",
    log: Progress = _noop,
) -> dict[str, dict[str, int]]:
    """Seed both sources. Verifies the fixture fingerprint before writing a row.

    The fingerprint check comes first deliberately: a drifted fixture makes
    every reference query in the suite wrong, and finding that out after
    twenty minutes of seeding and an hour of paid inference is the expensive
    way to learn it.
    """
    fixture = dataset.build_verified_fixture()
    log(f"fixture verified ({dataset.FINGERPRINT[:12]}…)")

    for spec in (targets.sales, targets.events):
        assert_safe_target(spec)

    ensure_database(targets.admin_dsn, targets.sales.database, log=log)
    result = {
        "sales": seed_postgres(
            targets.sales, fixture, SOURCE_TABLES["sales"], log=log
        )
    }

    if events_engine == "clickhouse":
        result["events"] = seed_clickhouse(
            targets.events, fixture, SOURCE_TABLES["events"], log=log
        )
    else:
        ensure_database(targets.admin_dsn, targets.events.database, log=log)
        result["events"] = seed_postgres(
            targets.events, fixture, SOURCE_TABLES["events"], log=log
        )
    return result

"""Where the fixture lives, and the tenant context the agents run against.

The eval warehouses are deliberately *not* the application database and *not*
the test database. Three separate concerns that all default to the same
compose Postgres on :5433 would otherwise be one typo away from the eval seeder
dropping a developer's reports table, so the guards in :func:`assert_safe_target`
are checked before any DDL runs -- the same refuse-to-run posture
``conftest.database_url`` takes for the test suite.

The context model is assembled here rather than read from Postgres. A real
workspace's model lives in ``datacontext_files`` and is materialized onto
``TenantContext`` by ``build_tenant_context``; the eval org has no rows in any
application table, so it builds the same frozen :class:`ContextModel` from files
on disk. Agents cannot tell the difference -- which is the point, since the
headline measurement is what that model is worth.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from uuid import UUID

from datatalk.config import Settings, get_settings
from datatalk.context import ContextFile, ContextModel, SourceRef, TenantContext
from datatalk.evals.dataset import SOURCE_TABLES
from datatalk.warehouse import WarehouseSpec

CONTEXT_DIR = Path(__file__).parent / "context"

# A distinct org id for eval runs, so a Langfuse project that also receives real
# traffic can filter these out (and so nothing here can be mistaken for a
# tenant). Never present in the application database.
EVAL_ORG_ID = UUID("00000000-0000-0000-0000-0000000000e5")

SOURCE_DESCRIPTIONS = {
    "sales": (
        "Transactional commerce database: customers, the product catalog, "
        "orders and their line items, and refunds."
    ),
    "events": (
        "Web analytics and marketing: raw page view events, and daily "
        "advertising spend per acquisition channel."
    ),
}


class UnsafeTargetError(RuntimeError):
    """The eval seeder was pointed at a database it must not touch."""


@dataclass(frozen=True)
class EvalTargets:
    """One :class:`WarehouseSpec` per eval source, plus the admin DSN."""

    sales: WarehouseSpec
    events: WarehouseSpec
    admin_dsn: str

    def spec(self, source: str) -> WarehouseSpec:
        return getattr(self, source)

    @property
    def source_names(self) -> tuple[str, ...]:
        return ("sales", "events")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def build_targets(*, events_engine: str = "postgres") -> EvalTargets:
    """Resolve eval warehouses from the environment.

    Defaults match ``docker compose up -d``, so the common case needs no
    configuration at all. ``events_engine="clickhouse"`` moves the events source
    onto ClickHouse, which is what turns the routing metric into a genuinely
    cross-engine one -- the agent then has to write a different dialect per
    source within a single question.
    """
    host = _env("DATATALK_EVAL_PG_HOST", "localhost")
    port = int(_env("DATATALK_EVAL_PG_PORT", "5433"))
    user = _env("DATATALK_EVAL_PG_USER", "datatalk")
    password = _env("DATATALK_EVAL_PG_PASSWORD", "datatalk")
    sales_db = _env("DATATALK_EVAL_SALES_DB", "datatalk_eval_sales")
    events_db = _env("DATATALK_EVAL_EVENTS_DB", "datatalk_eval_events")

    def pg(database: str) -> WarehouseSpec:
        return WarehouseSpec(
            type="postgres",
            host=host,
            port=port,
            username=user,
            password=password,
            database=database,
            introspect_databases=("public",),
            introspect_sample_rows=3,
        )

    sales = pg(sales_db)
    if events_engine == "clickhouse":
        events = WarehouseSpec(
            type="clickhouse",
            host=_env("DATATALK_EVAL_CH_HOST", "localhost"),
            port=int(_env("DATATALK_EVAL_CH_PORT", "8123")),
            username=_env("DATATALK_EVAL_CH_USER", "default"),
            password=_env("DATATALK_EVAL_CH_PASSWORD", ""),
            database=_env("DATATALK_EVAL_CH_DATABASE", "datatalk_eval_events"),
            secure=_env("DATATALK_EVAL_CH_SECURE", "false").lower() == "true",
            introspect_databases=(_env("DATATALK_EVAL_CH_DATABASE", "datatalk_eval_events"),),
            introspect_sample_rows=3,
        )
    elif events_engine == "postgres":
        events = pg(events_db)
    else:
        raise ValueError(f"Unknown events engine {events_engine!r} (postgres | clickhouse)")

    admin = (
        f"postgresql://{user}:{password}@{host}:{port}/"
        f"{_env('DATATALK_EVAL_PG_ADMIN_DB', 'postgres')}"
    )
    return EvalTargets(sales=sales, events=events, admin_dsn=admin)


def postgres_dsn(spec: WarehouseSpec) -> str:
    return (
        f"postgresql://{spec.username}:{spec.password}@{spec.host}:{spec.port}"
        f"/{spec.database}"
    )


def assert_safe_target(spec: WarehouseSpec) -> None:
    """Refuse to seed anything that might be a real database.

    The seeder drops and recreates every table it owns. Getting this wrong on
    the application database would destroy a developer's workspaces; getting it
    wrong on the test database would make the whole suite mysteriously flaky.
    Cheap to check, unrecoverable to skip.
    """
    if spec.type != "postgres":
        return
    forbidden: list[tuple[str, str]] = []
    for var in ("DATABASE_URL", "DATATALK_TEST_DATABASE_URL"):
        url = os.environ.get(var)
        if url:
            forbidden.append((var, url.rsplit("/", 1)[-1].split("?")[0]))
    for var, database in forbidden:
        if database and database == spec.database:
            raise UnsafeTargetError(
                f"Refusing to seed {spec.database!r}: it is the database {var} "
                "points at, and seeding drops every table it owns. Point "
                "DATATALK_EVAL_SALES_DB / DATATALK_EVAL_EVENTS_DB somewhere else."
            )
    if not spec.database.startswith("datatalk_eval"):
        raise UnsafeTargetError(
            f"Refusing to seed {spec.database!r}: eval databases must be named "
            "datatalk_eval*, so an accidental target is impossible to mistake "
            "for a real one."
        )


# --- the workspace context model ---------------------------------------------


def load_context_model() -> ContextModel:
    """The curated context model shipped with the suite.

    This is the treatment arm of the headline experiment. It says what
    ``revenue`` means, which statuses count, and where each entity lives --
    exactly the knowledge a schema cannot carry and the thing this product
    exists to inject. Running the same suite with and without it is the only
    honest way to put a number on that claim.
    """
    manifest_path = CONTEXT_DIR / "manifest.json"
    if not manifest_path.exists():
        return ContextModel()
    manifest = json.loads(manifest_path.read_text())
    files: list[ContextFile] = []
    for i, entry in enumerate(manifest.get("files", []), start=1):
        body_path = CONTEXT_DIR / entry["path"]
        files.append(
            ContextFile(
                id=i,
                path=entry["path"],
                summary=entry.get("summary", ""),
                body_md=body_path.read_text(),
                origin="human",
                covers=tuple(
                    (c["source"], c["table"]) for c in entry.get("covers", [])
                ),
            )
        )
    return ContextModel(files=tuple(files))


def build_eval_context(
    targets: EvalTargets,
    *,
    with_context_model: bool = True,
    sources: Iterable[str] = ("sales", "events"),
    settings: Settings | None = None,
) -> TenantContext:
    """The context the agents run against -- built exactly like a real one.

    ``TenantContext.from_sources`` is the same constructor
    ``auth.orgs.build_tenant_context`` uses once it has read the org's rows, so
    an agent driven by this context resolves warehouses, fingerprints and the
    context model through the identical code paths a request does. Nothing about
    the run is eval-shaped except where the rows came from.
    """
    refs = tuple(
        SourceRef.from_spec(
            targets.spec(name),
            name=name,
            description=SOURCE_DESCRIPTIONS.get(name, ""),
            is_default=(i == 0),
        )
        for i, name in enumerate(sources)
    )
    return TenantContext.from_sources(
        refs,
        org_id=EVAL_ORG_ID,
        org_slug="datatalk-evals",
        user_email="evals@datatalk.local",
        settings=settings or get_settings(),
        context_model=load_context_model() if with_context_model else ContextModel(),
    )


def expected_tables(source: str) -> tuple[str, ...]:
    return SOURCE_TABLES[source]

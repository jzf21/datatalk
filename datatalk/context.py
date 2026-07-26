"""The tenant context -- the single handle through which code reaches an org's data.

The rule this module exists to enforce:

    No module may reach a warehouse, an OpenAI client, or the schema cache
    except through a TenantContext.

Enforced structurally rather than by convention: the zero-argument
``get_client()`` and ``get_openai()`` singletons are gone, so a call site that
forgets to thread a context is an import error or a ``TypeError``, not a silent
cross-tenant read.

An org has **several** data sources, each a ClickHouse or Postgres warehouse.
The context carries all of them; the agent names one per query. There is no
"current" source outside the default used when a caller names none, and nothing
here knows which engine a source is -- that lives behind
:class:`~datatalk.warehouse.base.Warehouse`.

``TenantContext`` is frozen and holds **no database session and no store**. That
is deliberate: a SQLAlchemy ``Session`` is not thread-safe, and the agent worker
threads never need one. Keeping the session out makes "is this safe to hand to a
thread?" answerable from the type alone. The request-scoped bundle that *does*
carry a session is ``RequestContext`` in :mod:`datatalk.web.deps`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from openai import OpenAI

from datatalk import clients
from datatalk.config import Settings, get_settings
from datatalk.warehouse import Warehouse, WarehouseSpec
from datatalk.warehouse import fingerprint as spec_fingerprint

# Identifies the synthetic single-tenant context used by CLI scripts and tests.
# A real org never has this id.
ENV_ORG_ID = UUID("00000000-0000-0000-0000-000000000000")
ENV_USER_ID = UUID("00000000-0000-0000-0000-000000000001")

_EMPTY_OVERRIDES: Mapping[str, Warehouse] = MappingProxyType({})


class NoConnectionError(RuntimeError):
    """Raised when an org reaches for a warehouse before configuring any source.

    The env ``CLICKHOUSE_*`` values are the *deployment's* warehouse, not any
    org's. Falling back to them for an org with no stored source would serve one
    tenant another's data, so the fallback does not exist -- structurally, not
    by convention: a connectionless org's context has an empty ``sources``
    tuple, so there is nothing for :meth:`TenantContext.warehouse` to resolve
    and no code path that could reach the environment.
    :func:`datatalk.web.deps.require_connection` turns this into a 409 up front;
    this is the backstop for any path that skips it.
    """

    def __init__(self, org_id: UUID | None = None) -> None:
        super().__init__(
            "This workspace has no data source configured. "
            "Add one under Settings -> Sources."
        )
        self.org_id = org_id


class UnknownSourceError(LookupError):
    """Raised when a caller names a source the org does not have.

    Usually the LLM inventing a source name. The tool loop turns this into a
    tool-result error the model can recover from, listing what does exist -- it
    is never a 500.
    """

    def __init__(self, name: str, available: tuple[str, ...] = ()) -> None:
        known = ", ".join(available) if available else "(none)"
        super().__init__(f"Unknown source {name!r}. Configured sources: {known}.")
        self.name = name
        self.available = available


@dataclass(frozen=True)
class SourceRef:
    """One of an org's data sources, resolved from its stored connection row.

    Carries no live client: :attr:`spec` plus :attr:`fingerprint` are enough for
    the registry to hand back the shared warehouse, which keeps this frozen and
    thread-safe.
    """

    id: UUID | None
    name: str  # the handle the model types in run_sql(source=...)
    type: str
    description: str
    is_default: bool
    spec: WarehouseSpec
    fingerprint: str

    @classmethod
    def from_spec(
        cls,
        spec: WarehouseSpec,
        *,
        name: str = "default",
        id: UUID | None = None,
        description: str = "",
        is_default: bool = True,
    ) -> "SourceRef":
        return cls(
            id=id,
            name=name,
            type=spec.type,
            description=description,
            is_default=is_default,
            spec=spec,
            fingerprint=spec_fingerprint(spec),
        )


@dataclass(frozen=True)
class ContextFile:
    """One markdown file of the org's curated context model.

    Frozen and plain, like :class:`SourceRef`: it is handed to agent worker
    threads, which have no database session, so it must not lazy-load anything.
    """

    id: int
    path: str
    summary: str
    body_md: str
    origin: str = "agent"
    covers: tuple[tuple[str, str], ...] = ()  # (source_name, qualified_table)


@dataclass(frozen=True)
class ContextModel:
    """An org's whole context model, as a snapshot.

    Handed to a worker thread on the frozen :class:`TenantContext`. A run that
    takes minutes keeps the snapshot it started with even if an admin edits a
    file meanwhile -- the same consistency ``sources`` already gives, and the
    reason nothing here reaches back to Postgres.
    """

    files: tuple[ContextFile, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.files

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files)

    def get(self, path: str) -> ContextFile | None:
        """Resolve a path the model typed. Forgiving, like ``describe_table``.

        Exact, then normalized (a leading ``context/``, a missing ``.md``), then
        an unambiguous bare name (``churn`` -> ``playbooks/churn.md``). A weak
        model mistypes paths, and a wasted tool step costs far more than these
        comparisons.
        """
        wanted = (path or "").strip().strip("`\"'").lstrip("/")
        if not wanted:
            return None

        by_path = {f.path: f for f in self.files}
        if wanted in by_path:
            return by_path[wanted]

        normalized = wanted.removeprefix("context/")
        if not normalized.endswith(".md"):
            normalized += ".md"
        lowered = normalized.lower()
        for f in self.files:
            if f.path.lower() == lowered:
                return f

        # Bare name, e.g. "churn" -> "playbooks/churn.md". Ambiguity means the
        # model has to be specific; guessing between two files would be worse
        # than an error naming both.
        stem = lowered.rsplit("/", 1)[-1]
        matches = [f for f in self.files if f.path.lower().rsplit("/", 1)[-1] == stem]
        return matches[0] if len(matches) == 1 else None

    def covering(self, source: str, table: str) -> tuple[ContextFile, ...]:
        """Files documenting one table, matched on qualified or bare name."""
        wanted = (table or "").strip().strip("`\"'").lower()
        if not wanted:
            return ()
        bare = wanted.rsplit(".", 1)[-1]

        hits = []
        for f in self.files:
            for src, covered in f.covers:
                if source and src and src != source:
                    continue
                covered = covered.lower()
                if covered == wanted or covered.rsplit(".", 1)[-1] == bare:
                    hits.append(f)
                    break
        return tuple(hits)

    def render_tree(self) -> str:
        """The always-on block: one line per file, never a body.

        This is the whole two-tier bargain. Bodies reach the model only through
        ``read_context``, so adding a file costs ~35 tokens of prompt, not a
        page.
        """
        if not self.files:
            return ""
        lines = [
            "=== WORKSPACE CONTEXT MODEL ===",
            "Curated documentation of this workspace's data, written and maintained",
            "by this workspace. It is authoritative for what things MEAN; the",
            "catalog below is authoritative for which columns EXIST.",
            "",
            "Read a file with the `read_context` tool before deciding which table",
            "holds an entity, what a metric means, or how a question of this shape",
            "is normally answered here. Do not guess when a file covers it.",
            "",
        ]
        for f in self.files:
            line = f.path
            if f.summary:
                line += f" — {f.summary}"
            lines.append(line)
        lines.append("=== END CONTEXT MODEL ===")
        return "\n".join(lines)


EMPTY_CONTEXT_MODEL = ContextModel()


@dataclass(frozen=True)
class TenantContext:
    """Immutable compute context for one org acting as one user.

    Safe to pass into a worker thread: every field is a plain value, and the
    client accessors go through the locked registry in :mod:`datatalk.clients`.
    """

    org_id: UUID
    org_slug: str
    user_id: UUID | None
    user_email: str
    role: str  # owner | admin | member | viewer
    # Environment defaults only: OpenAI credentials and the guardrail ceilings a
    # source inherits when it sets no override. Nothing warehouse-specific --
    # that is per source, on SourceRef.spec.
    settings: Settings
    # The org's data sources, ordered default-first then by name. Empty for an
    # org that has configured none.
    sources: tuple[SourceRef, ...] = ()

    # The org's curated documentation, loaded once on the request thread (which
    # has a session) so the worker threads that read it never need one.
    context_model: ContextModel = EMPTY_CONTEXT_MODEL

    # Injection points for tests and the connection-test endpoint. Never set on
    # a context built from a real request. Keyed by source name.
    openai_override: OpenAI | None = None
    warehouse_overrides: Mapping[str, Warehouse] = field(default=_EMPTY_OVERRIDES)

    @property
    def openai(self) -> OpenAI:
        if self.openai_override is not None:
            return self.openai_override
        return clients.openai_for(self.settings)

    @property
    def model(self) -> str:
        return self.settings.openai_model

    @property
    def docs_openai(self) -> OpenAI:
        """Client for the documentation agent, which may use a second endpoint.

        The test override wins here too, so one scripted fake still drives the
        whole pipeline.
        """
        if self.openai_override is not None:
            return self.openai_override
        return clients.openai_for(self.settings.docs_openai_settings)

    @property
    def docs_model(self) -> str:
        """The stronger model reserved for documentation generation.

        That job runs rarely, over a whole warehouse, and its output lands in
        every later prompt -- so it is worth more than the per-report loop's
        model. Falls back to :attr:`model` when unconfigured.
        """
        return self.settings.docs_model

    @property
    def has_connection(self) -> bool:
        """False for an org that has configured no data source at all."""
        return bool(self.sources)

    @property
    def source_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.sources)

    @property
    def fingerprint(self) -> str:
        """Identity of the whole source set; keys the combined schema catalog.

        Individual sources keep their own fingerprints for the client registry
        and per-source schema cache, so editing one source does not invalidate
        the others' introspection.
        """
        raw = "|".join(f"{s.name}:{s.fingerprint}" for s in self.sources)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def source(self, name: str | None = None) -> SourceRef:
        """Resolve a source by name; ``None`` means the default one."""
        if not self.sources:
            raise NoConnectionError(self.org_id)
        if name is None or name == "":
            return self.sources[0]
        for ref in self.sources:
            if ref.name == name:
                return ref
        raise UnknownSourceError(name, self.source_names)

    def warehouse(self, name: str | None = None) -> Warehouse:
        """Return the shared warehouse for one source.

        Resolves the name first, so an override cannot mask an unknown-source
        error and tests exercise the same lookup real requests do.
        """
        ref = self.source(name)
        override = self.warehouse_overrides.get(ref.name)
        if override is not None:
            return override
        return clients.warehouse_for(ref.spec, ref.fingerprint)

    def with_sources(self, sources: tuple[SourceRef, ...]) -> "TenantContext":
        return replace(self, sources=sources)

    def with_context_model(self, context_model: ContextModel) -> "TenantContext":
        return replace(self, context_model=context_model)

    @classmethod
    def from_sources(
        cls,
        sources: tuple[SourceRef, ...],
        *,
        org_id: UUID,
        org_slug: str = "",
        user_id: UUID | None = None,
        user_email: str = "",
        role: str = "owner",
        settings: Settings | None = None,
        context_model: ContextModel = EMPTY_CONTEXT_MODEL,
    ) -> "TenantContext":
        return cls(
            org_id=org_id,
            org_slug=org_slug,
            user_id=user_id,
            user_email=user_email,
            role=role,
            settings=settings or get_settings(),
            sources=sources,
            context_model=context_model,
        )

    @classmethod
    def from_env(cls) -> "TenantContext":
        """Single-tenant context built purely from the environment.

        Used by CLI scripts (``datatalk-check``) and as the bootstrap source for
        an org's first connection. Carries no real org identity. This is the one
        place the deployment's own ``CLICKHOUSE_*`` values are dialled, and it
        is never reachable from a request.
        """
        settings = get_settings()
        return cls.from_sources(
            (SourceRef.from_spec(env_spec(settings), name="default"),),
            org_id=ENV_ORG_ID,
            org_slug="env",
            user_id=ENV_USER_ID,
            role="owner",
            settings=settings,
        )

    @classmethod
    def for_test(
        cls,
        *,
        openai: OpenAI | None = None,
        warehouses: Mapping[str, Warehouse] | None = None,
        sources: tuple[SourceRef, ...] = (),
        settings: Settings | None = None,
        org_id: UUID = ENV_ORG_ID,
        org_slug: str = "test",
        user_id: UUID | None = ENV_USER_ID,
        user_email: str = "test@example.com",
        role: str = "owner",
        context_model: ContextModel = EMPTY_CONTEXT_MODEL,
    ) -> "TenantContext":
        """Build a context with fake clients -- one injection point for agent tests.

        ``warehouses`` maps source name to a stand-in implementing the
        :class:`~datatalk.warehouse.base.Warehouse` protocol. When ``sources``
        is not given, one ``SourceRef`` per key is synthesized so
        ``has_connection`` and ``source_names`` behave like the real thing.
        """
        settings = settings or get_settings()
        warehouses = dict(warehouses or {})
        if not sources and warehouses:
            sources = tuple(
                SourceRef.from_spec(
                    WarehouseSpec(
                        type=getattr(getattr(wh, "dialect", None), "name", "clickhouse"),
                        host=f"{name}.test",
                        port=0,
                        username="test",
                        database=name,
                    ),
                    name=name,
                    is_default=(i == 0),
                )
                for i, (name, wh) in enumerate(warehouses.items())
            )
        return cls(
            org_id=org_id,
            org_slug=org_slug,
            user_id=user_id,
            user_email=user_email,
            role=role,
            settings=settings,
            sources=sources,
            context_model=context_model,
            openai_override=openai,
            warehouse_overrides=MappingProxyType(warehouses),
        )


def env_spec(settings: Settings | None = None) -> WarehouseSpec:
    """The deployment's own warehouse, from ``CLICKHOUSE_*``.

    Bootstrap and CLI only. No request path may call this: see
    :class:`NoConnectionError`.
    """
    s = settings or get_settings()
    return WarehouseSpec(
        type="clickhouse",
        host=s.clickhouse_host,
        port=s.clickhouse_port,
        username=s.clickhouse_user,
        password=s.clickhouse_password,
        database=s.clickhouse_database,
        secure=s.clickhouse_secure,
        introspect_databases=tuple(s.introspect_database_list),
        introspect_exclude_patterns=tuple(s.introspect_exclude_list),
        introspect_sample_rows=s.introspect_sample_rows,
        introspect_max_tables=s.introspect_max_tables,
        sql_default_limit=s.sql_default_limit,
        sql_max_rows=s.sql_max_rows,
        sql_timeout_seconds=s.sql_timeout_seconds,
    )

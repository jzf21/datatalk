"""The tenant context -- the single handle through which code reaches an org's data.

The rule this module exists to enforce:

    No module may reach a ClickHouse client, an OpenAI client, or the schema
    cache except through a TenantContext.

Enforced structurally rather than by convention: the zero-argument
``get_client()`` and ``get_openai()`` singletons are gone, so a call site that
forgets to thread a context is an import error or a ``TypeError``, not a silent
cross-tenant read.

``TenantContext`` is frozen and holds **no database session and no store**. That
is deliberate: a SQLAlchemy ``Session`` is not thread-safe, and the agent worker
threads never need one. Keeping the session out makes "is this safe to hand to a
thread?" answerable from the type alone. The request-scoped bundle that *does*
carry a session is ``RequestContext`` in :mod:`datatalk.web.deps`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from uuid import UUID

from clickhouse_connect.driver.client import Client
from openai import OpenAI

from datatalk import clients
from datatalk.config import Settings, get_settings

# Identifies the synthetic single-tenant context used by CLI scripts and tests.
# A real org never has this id.
ENV_ORG_ID = UUID("00000000-0000-0000-0000-000000000000")
ENV_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


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
    # Env defaults with the org's ClickHouse connection overlaid on top.
    settings: Settings
    # Hash of the connection + introspection tuple. Keys the client registry and
    # the schema cache, so changing credentials invalidates both automatically.
    fingerprint: str

    # Injection points for tests and the connection-test endpoint. Never set on
    # a context built from a real request.
    openai_override: OpenAI | None = None
    clickhouse_override: Client | None = None

    @property
    def openai(self) -> OpenAI:
        if self.openai_override is not None:
            return self.openai_override
        return clients.openai_for(self.settings)

    @property
    def clickhouse(self) -> Client:
        if self.clickhouse_override is not None:
            return self.clickhouse_override
        return clients.clickhouse_for(self.settings, self.fingerprint)

    @property
    def model(self) -> str:
        return self.settings.openai_model

    def with_settings(self, settings: Settings) -> "TenantContext":
        """Return a copy bound to different settings, refingerprinted."""
        return replace(
            self, settings=settings, fingerprint=clients.fingerprint(settings)
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        org_id: UUID,
        org_slug: str = "",
        user_id: UUID | None = None,
        user_email: str = "",
        role: str = "owner",
    ) -> "TenantContext":
        return cls(
            org_id=org_id,
            org_slug=org_slug,
            user_id=user_id,
            user_email=user_email,
            role=role,
            settings=settings,
            fingerprint=clients.fingerprint(settings),
        )

    @classmethod
    def from_env(cls) -> "TenantContext":
        """Single-tenant context built purely from the environment.

        Used by CLI scripts (``datatalk-check``) and as the bootstrap source for
        the first org's ClickHouse connection. Carries no real org identity.
        """
        return cls.from_settings(
            get_settings(),
            org_id=ENV_ORG_ID,
            org_slug="env",
            user_id=ENV_USER_ID,
            role="owner",
        )

    @classmethod
    def for_test(
        cls,
        *,
        openai: OpenAI | None = None,
        clickhouse: Client | None = None,
        settings: Settings | None = None,
        org_id: UUID = ENV_ORG_ID,
        org_slug: str = "test",
        user_id: UUID | None = ENV_USER_ID,
        user_email: str = "test@example.com",
        role: str = "owner",
    ) -> "TenantContext":
        """Build a context with fake clients -- one injection point for agent tests.

        Replaces monkeypatching ``get_openai`` on each agent module separately.
        """
        settings = settings or get_settings()
        return cls(
            org_id=org_id,
            org_slug=org_slug,
            user_id=user_id,
            user_email=user_email,
            role=role,
            settings=settings,
            fingerprint=clients.fingerprint(settings),
            openai_override=openai,
            clickhouse_override=clickhouse,
        )

"""Langfuse tracing — the one place this codebase knows the tracer exists.

Every agent module reaches tracing through the helpers here, never through
``langfuse`` directly. Two reasons, both structural:

* **Tracing is optional.** With no ``LANGFUSE_*`` credentials configured (tests,
  CLI scripts, a deployment that does not want it) every helper below degrades
  to a no-op object, so agent code carries no ``if tracing:`` branches and the
  package still imports with ``langfuse`` uninstalled.
* **Credentials come from ``.env``, which the Langfuse SDK cannot see.** The SDK
  reads ``os.environ``; DataTalk's configuration is a pydantic-settings
  ``Settings`` loaded from a local ``.env``. So the client is built *explicitly*
  from :class:`~datatalk.config.Settings` in :func:`configure`, never implicitly
  from the environment.

What gets traced, and by whom:

* **LLM calls** are captured automatically. Importing ``langfuse.openai``
  patches ``openai.resources.chat.completions.Completions.create`` process-wide
  via ``wrapt``, so every client :mod:`datatalk.clients` hands out -- the report
  model, the docs model, the author model -- emits a ``generation`` carrying the
  model name, token usage and cost without any call site changing. That patch is
  installed in :func:`configure` and *only* when tracing is on.
* **Structure** is ours: the Planner/Analyst/Reporter pipeline, the tool calls
  inside the ``run_sql`` loop, and the trace-level attributes are spans this
  module opens. See :func:`observe` and :func:`child`.

Multi-tenancy: :func:`tenant_attributes` puts the acting user on ``user_id`` and
the org on a tag plus metadata, so a trace can be filtered back to one workspace.
The user's **id**, never their email -- traces should not become a PII store.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager, nullcontext
from typing import TYPE_CHECKING, Any, Iterator

if TYPE_CHECKING:
    from datatalk.config import Settings
    from datatalk.context import TenantContext

logger = logging.getLogger(__name__)

# Observation types Langfuse understands. Using the *specific* one (``tool`` for
# a tool call, ``agent`` for a subagent, ``retriever`` for a lookup) rather than
# a generic span is what drives the agent graph and lets evaluators target a
# step by type; see https://langfuse.com/docs/observability/features/observation-types
SPAN = "span"
AGENT = "agent"
TOOL = "tool"
RETRIEVER = "retriever"
GENERATION = "generation"
EMBEDDING = "embedding"

# Substrings that mark a mapping key as carrying a secret. Applied to every
# input/output the SDK serializes, so a warehouse spec or a settings object that
# reaches a span cannot ship a password to a third party.
_SECRET_HINTS = (
    "password",
    "secret",
    "api_key",
    "apikey",
    "access_key",
    "token",
    "authorization",
    "credential",
)
_REDACTED = "<redacted>"
_MASK_MAX_DEPTH = 12

# Observations that are pure noise in a trace list. ``/api/health`` pings the
# model on every poll of the connection pill, so one open browser tab produces a
# trace every ~30 seconds -- burying real runs and skewing every dashboard that
# counts traces. It is dropped at export rather than left un-instrumented so the
# name still exists at the call site and the reason lives in one place.
_UNEXPORTED_NAMES = frozenset({"health-check"})

_LOCK = threading.RLock()
_configured = False
_enabled = False
_client: Any = None
_capture_rows = True


class _NullSpan:
    """Stand-in for a Langfuse observation when tracing is off.

    Absorbs the whole observation surface the agents use, so a call site reads
    identically whether or not tracing is configured.
    """

    __slots__ = ()

    def update(self, **kwargs: Any) -> "_NullSpan":
        return self

    def end(self, **kwargs: Any) -> "_NullSpan":
        return self

    def start_observation(self, **kwargs: Any) -> "_NullSpan":
        return self

    def start_as_current_observation(self, **kwargs: Any) -> Any:
        return nullcontext(self)

    def score(self, *args: Any, **kwargs: Any) -> "_NullSpan":
        return self

    def __bool__(self) -> bool:
        return False


NULL_SPAN = _NullSpan()


# --- configuration ------------------------------------------------------------


def _release() -> str | None:
    try:
        from importlib.metadata import version

        return version("datatalk")
    except Exception:  # noqa: BLE001 - a missing dist is not worth failing over
        return None


def configure(settings: "Settings | None" = None, *, force: bool = False) -> bool:
    """Build the process-wide Langfuse client. Idempotent; returns whether it is on.

    Called once from the FastAPI lifespan. Safe to call from a script. Any
    failure -- missing package, bad credentials, unreachable host -- logs and
    leaves tracing off rather than taking the application down with it:
    observability must never be the reason a report cannot be generated.

    ``force`` re-runs configuration (tests only); the underlying SDK keeps one
    client per public key, so repeated real calls would be wasted work.
    """
    global _configured, _enabled, _client, _capture_rows

    with _LOCK:
        if _configured and not force:
            return _enabled
        _configured = True
        _enabled = False
        _client = None

        from datatalk.config import get_settings

        s = settings or get_settings()
        _capture_rows = s.langfuse_capture_row_values

        if not s.has_langfuse:
            logger.debug("Langfuse tracing off (no credentials configured).")
            return False

        try:
            from langfuse import Langfuse

            client = Langfuse(
                public_key=s.langfuse_public_key,
                secret_key=s.langfuse_secret_key,
                base_url=s.langfuse_base_url or None,
                environment=s.langfuse_environment or None,
                release=_release(),
                sample_rate=s.langfuse_sample_rate,
                debug=s.langfuse_debug,
                mask=_mask,
                # has_langfuse has already decided this from .env. Note the SDK
                # lets a ``LANGFUSE_TRACING_ENABLED=false`` in the *process*
                # environment override this argument, which is the escape hatch
                # an operator wants: kill tracing without editing config.
                tracing_enabled=True,
                should_export_span=_should_export_span,
            )
            # Patches the OpenAI SDK process-wide (wrapt, on the resource class),
            # so clients built before this import are instrumented too. Imported
            # here and not at module scope: with tracing off, nothing about the
            # OpenAI call path should change.
            import langfuse.openai  # noqa: F401
        except Exception:  # noqa: BLE001 - tracing must never break the app
            logger.warning("Langfuse tracing could not be configured.", exc_info=True)
            return False

        _client = client
        _enabled = True
        logger.info(
            "Langfuse tracing on (%s, environment=%s).",
            s.langfuse_base_url or "cloud.langfuse.com",
            s.langfuse_environment or "default",
        )
        return True


def is_enabled() -> bool:
    return _enabled


def client() -> Any:
    """The configured Langfuse client, or ``None``."""
    return _client


def flush() -> None:
    """Send everything buffered. For short-lived processes and tests."""
    if _client is not None:
        try:
            _client.flush()
        except Exception:  # noqa: BLE001
            logger.debug("Langfuse flush failed.", exc_info=True)


def shutdown() -> None:
    """Flush and stop the background threads. Called from the app lifespan."""
    global _client, _enabled, _configured
    with _LOCK:
        if _client is not None:
            try:
                _client.shutdown()
            except Exception:  # noqa: BLE001
                logger.debug("Langfuse shutdown failed.", exc_info=True)
        _client = None
        _enabled = False
        _configured = False


# --- masking ------------------------------------------------------------------


def _should_export_span(span: Any) -> bool:
    """Drop health-probe spans before they reach Langfuse.

    Never raises: an exception here would be raised inside the SDK's exporter,
    on a background thread, where it is invisible. Default to exporting.
    """
    try:
        return getattr(span, "name", "") not in _UNEXPORTED_NAMES
    except Exception:  # noqa: BLE001
        return True


def _mask(*, data: Any, **_: Any) -> Any:
    """Redact credential-shaped values from anything the SDK is about to send.

    Runs on every observation input/output. Structural, not heuristic: it looks
    at mapping *keys*, so it catches a ``WarehouseSpec`` or a settings dump that
    reaches a span by accident, and leaves ordinary analytical values alone.

    Never raises. A mask that throws would drop the observation entirely, which
    is a worse failure than a slightly over-redacted one.
    """
    try:
        return _redact(data, 0)
    except Exception:  # noqa: BLE001
        return "<masking failed>"


def _redact(value: Any, depth: int) -> Any:
    if depth > _MASK_MAX_DEPTH:
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k).lower()
            if any(hint in key for hint in _SECRET_HINTS):
                out[k] = _REDACTED
            else:
                out[k] = _redact(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v, depth + 1) for v in value]
    return value


def capture_row_values() -> bool:
    """Whether warehouse row *values* may be recorded on tool observations.

    Off (``LANGFUSE_CAPTURE_ROW_VALUES=false``) a ``run_sql`` observation still
    records the SQL, the columns and the row count -- everything needed to see
    what the agent asked and how much came back -- but not the tenant's actual
    numbers. The prompts sent to the model still contain them; that is inherent
    to tracing an LLM, and is what Langfuse's own masking is for.
    """
    return _capture_rows


# --- observations -------------------------------------------------------------


@contextmanager
def observe(
    name: str,
    *,
    as_type: str = SPAN,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Iterator[Any]:
    """Open an observation and make it the active parent for its block.

    Yields the observation (or :data:`NULL_SPAN` when tracing is off), so the
    body can ``.update(output=...)``. Names are an API -- evaluators, dashboards
    and saved filters target them -- so they are verbs and carry no run-specific
    values; put those in ``metadata``.
    """
    if not _enabled or _client is None:
        yield NULL_SPAN
        return
    try:
        cm = _client.start_as_current_observation(
            name=name, as_type=as_type, input=input, metadata=metadata, **kwargs
        )
    except Exception:  # noqa: BLE001
        logger.debug("Could not start observation %r.", name, exc_info=True)
        yield NULL_SPAN
        return
    with cm as span:
        yield span


def start(
    name: str,
    *,
    as_type: str = SPAN,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Start an observation under the active one *without* becoming active.

    The caller **must** ``.end()`` it. This is what the batched ``run_sql``
    calls in :mod:`datatalk.agent.sqlloop` use: OpenTelemetry's active context
    does not cross a ``ThreadPoolExecutor``, so the observation is opened here,
    on the loop thread where the parent is still active, and then updated and
    ended from the worker thread that ran the query. The batch is submitted in
    one go, so opening them together is also the honest start time.
    """
    if not _enabled or _client is None:
        return NULL_SPAN
    try:
        return _client.start_observation(
            name=name, as_type=as_type, input=input, metadata=metadata, **kwargs
        )
    except Exception:  # noqa: BLE001
        logger.debug("Could not start observation %r.", name, exc_info=True)
        return NULL_SPAN


@contextmanager
def tenant_attributes(
    ctx: "TenantContext",
    *,
    feature: str,
    session_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Iterator[None]:
    """Attach who/where to every observation opened inside the block.

    ``user_id`` is the user's **id**, not their email: a trace store should not
    become a directory of customer addresses. The org travels as both a tag
    (the business dimension you break a dashboard down by) and metadata (the
    exact id you filter on when chasing one workspace's bad run).
    """
    if not _enabled:
        yield
        return
    try:
        from langfuse import propagate_attributes
    except Exception:  # noqa: BLE001
        yield
        return

    # Scalars, not lists: trace-level metadata is flattened for filtering, so a
    # list arrives in the UI as the repr of a Python list and cannot be matched
    # on. Comma-joined reads correctly and still supports a contains filter.
    metadata: dict[str, Any] = {
        "org_id": str(ctx.org_id),
        "org_slug": ctx.org_slug,
        "role": ctx.role,
        "source_count": len(ctx.sources),
        "sources": ", ".join(ctx.source_names),
        "source_types": ", ".join(sorted({s.type for s in ctx.sources})),
        "model": ctx.model,
        "context_files": len(ctx.context_model.files),
    }
    if extra:
        metadata.update(extra)

    tags = [f"feature:{feature}"]
    if ctx.org_slug:
        tags.append(f"org:{ctx.org_slug}")

    try:
        cm = propagate_attributes(
            user_id=str(ctx.user_id) if ctx.user_id else None,
            session_id=session_id,
            tags=tags,
            metadata=metadata,
        )
    except Exception:  # noqa: BLE001
        yield
        return
    with cm:
        yield


@contextmanager
def agent_run(
    name: str,
    ctx: "TenantContext",
    *,
    feature: str,
    input: Any = None,
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """The root observation of one user-facing run, with tenant attributes set.

    One trace per self-contained unit of work -- one report, one dashboard, one
    Q&A turn -- which is the scope Langfuse's data model is built around. The
    yielded span's input/output become the trace's, so callers set an output a
    reviewer can read at a glance rather than leaving it to whatever the last
    step returned.

    Opened on whichever thread does the work: the streaming endpoints run their
    generation on a ``threading.Thread``, and OpenTelemetry's active context
    does not follow one.
    """
    with observe(name, as_type=AGENT, input=input, metadata=metadata) as root:
        with tenant_attributes(
            ctx, feature=feature, session_id=session_id
        ):
            yield root


def llm_kwargs(name: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Extra ``chat.completions.create`` kwargs the Langfuse wrapper consumes.

    Returns ``{}`` when tracing is off — the plain OpenAI SDK rejects unknown
    keyword arguments, so these may only be passed while the wrapper that strips
    them is installed. Naming the generation is what keeps the trace tree
    readable: without it every LLM call in the pipeline is
    ``OpenAI-generation``.
    """
    if not _enabled:
        return {}
    kwargs: dict[str, Any] = {"name": name}
    if metadata:
        kwargs["metadata"] = metadata
    return kwargs

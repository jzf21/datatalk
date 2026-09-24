"""FastAPI web layer (Milestone 6).

Thin wrapper over the agent/memory packages. Report generation streams progress
as newline-delimited JSON (NDJSON) so the UI can show queries as they run.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from datatalk import clients
from datatalk import jsonsafe
from datatalk import observability as obs
from datatalk.dashboards import configure as configure_svc
from datatalk.dashboards import refresh as refresh_svc
from datatalk.dashboards import layout as layout_svc
from datatalk.dashboards.filters import FilterError, coerce_values
from datatalk.dashboards import templates as templates_svc
from datatalk.dashboards.templates import instantiate as instantiate_svc
from datatalk.agent.analyze import analyze_dashboard, analyze_report
from datatalk.agent.blocks import Document
from datatalk.agent.dashboard import generate_dashboard, generate_widget
from datatalk.agent.qa import answer_question
from datatalk.agent.report import generate_report
from datatalk.auth import orgs as orgs_svc
from datatalk.auth import sessions as sessions_svc
from datatalk.config import get_settings
from datatalk.db.session import get_engine, session_scope
from datatalk.llm import client as llm_client
from datatalk.memory.store import MemoryStore
from datatalk.warehouse.catalog import build_catalog
from datatalk.web import routes_auth, routes_datacontext, routes_orgs
from datatalk.web.streaming import drain, ndjson
from datatalk.context import NoConnectionError
from datatalk.web.deps import (
    RequestContext,
    csrf_guard,
    get_current_user,
    get_request_ctx,
    require_connection,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Fail fast on a missing or out-of-date database, not on first request."""
    from datatalk.scripts.db import alembic_config, schema_is_current

    settings = get_settings()

    # Before any request can reach the agents. Configuring it installs the
    # Langfuse patch on the OpenAI SDK; with no LANGFUSE_* credentials this is a
    # no-op and the OpenAI call path is untouched. Never fatal: an observability
    # backend must not be able to stop reports from being generated.
    obs.configure(settings)

    engine = get_engine()
    with engine.connect():
        pass  # surfaces an unreachable database immediately

    if settings.db_auto_migrate:
        from alembic import command

        command.upgrade(alembic_config(), "head")
    elif not schema_is_current(engine):
        raise RuntimeError(
            "Database schema is out of date. Run:  datatalk-db upgrade"
        )

    # Deliberately not in the try below: a bad bootstrap password is a config
    # error, and starting anyway would leave a closed-signup deployment with no
    # way in at all.
    with session_scope() as db:
        made = orgs_svc.bootstrap_from_settings(db, settings)
    if made:
        logger.info("Bootstrap created: %s.", made)

    try:
        with session_scope() as db:
            swept = sessions_svc.sweep_expired(db)
        if swept:
            logger.info("Swept %d expired session(s).", swept)
    except Exception:  # noqa: BLE001 - never block startup on housekeeping
        logger.warning("Expired-session sweep failed.", exc_info=True)

    yield

    # Before the process goes away: the SDK batches in the background, so a
    # report finished seconds before shutdown would otherwise never be sent.
    obs.shutdown()
    clients.close_all()
    engine.dispose()


app = FastAPI(title="DataTalk", version="0.1.0", lifespan=lifespan)

# The frontend runs on its own origin, so the browser preflights every JSON POST.
# Read at import time because middleware must be registered before startup.
_cors_origins = get_settings().cors_origin_list
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        # Required for cookie auth from a cross-origin frontend. Safe ONLY
        # because allow_origins is an exact allowlist -- never pair credentials
        # with a wildcard or a reflected origin.
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
        max_age=3600,
    )

@app.exception_handler(NoConnectionError)
def _no_connection_handler(request: Request, exc: NoConnectionError) -> JSONResponse:
    """Backstop for a connectionless org that got past ``require_connection``.

    The dependency covers the endpoints we know reach ClickHouse; this covers
    the ones we forget. Same 409 + ``no_connection`` code either way, so the
    frontend has one branch to write.
    """
    return JSONResponse(status_code=409, content={"detail": "no_connection"})


# Every application endpoint hangs off this router, so authentication is
# structural: a new route added here cannot forget its Depends.
api = APIRouter(
    prefix="/api", dependencies=[Depends(get_current_user), Depends(csrf_guard)]
)


# --- models ---

class ReportRequest(BaseModel):
    request: str
    use_memory: bool = True


class AnalyzeRequest(BaseModel):
    report_id: int | None = None
    text: str | None = None
    focus: str | None = None
    use_memory: bool = True


class DashboardRequest(BaseModel):
    request: str
    use_memory: bool = True


class DashboardAnalyzeRequest(BaseModel):
    focus: str | None = None
    use_memory: bool = True


class DashboardRefreshRequest(BaseModel):
    """Filter selections to apply, keyed by filter id. Empty = refresh as captured."""

    filters: dict[str, dict[str, Any]] = {}


class FilterDefRequest(BaseModel):
    """One filter to define. Options and templates are derived server-side."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    kind: Literal["date_range", "dimension"]
    label: str = Field(max_length=60)
    # dimension only
    column: str | None = Field(default=None, max_length=128)
    source: str | None = Field(default=None, max_length=64)
    multi: bool = True


class DashboardFiltersRequest(BaseModel):
    filters: list[FilterDefRequest] = Field(default_factory=list, max_length=8)


class DashboardFromTemplateRequest(BaseModel):
    template_id: str = Field(max_length=64)
    source: str = Field(max_length=128)
    title: str | None = Field(default=None, max_length=120)


class DashboardLayoutRequest(BaseModel):
    authoring_document: dict[str, Any]


class AddWidgetRequest(BaseModel):
    widget_key: str = Field(max_length=64)


class AIWidgetRequest(BaseModel):
    request: str = Field(max_length=2000)


class WidgetFiltersRequest(BaseModel):
    # The dashboard filters this dataset responds to; absent = unchanged.
    wired: list[str] | None = Field(default=None, max_length=16)
    # A selection pinned for this dataset alone, by filter id.
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


class FeedbackRequest(BaseModel):
    text: str


class AskRequest(BaseModel):
    question: str


# --- service descriptor ---

@app.get("/")
def index() -> dict[str, Any]:
    """This process serves the API only.

    The UI is the Next.js app in ``frontend/``, which runs on its own origin
    (see DATATALK_CORS_ORIGINS). A stray bookmark lands here, so point it
    somewhere useful rather than 404.
    """
    return {
        "name": "DataTalk API",
        "version": app.version,
        "docs": "/docs",
        "ui": get_settings().cors_origin_list[:1] or None,
    }


# --- health / schema (connection checkout) ---

def _check_source(ctx, ref) -> dict[str, Any]:
    """Ping one source. Never raises: a dead source is a reported state."""
    base = {"name": ref.name, "type": ref.type, "is_default": ref.is_default}
    try:
        warehouse = ctx.warehouse(ref.name)
        info = warehouse.ping()
        tables = warehouse.introspect(with_samples=False)
        return {
            **base,
            "ok": True,
            "version": info["version"],
            "database": info["database"],
            "table_count": len(tables),
        }
    except Exception as exc:  # noqa: BLE001
        return {**base, "ok": False, "error": str(exc)}


@api.get("/health")
def health(rctx: RequestContext = Depends(require_connection)) -> dict[str, Any]:
    ctx = rctx.tenant
    settings = ctx.settings
    result: dict[str, Any] = {"sources": [], "openai": {"ok": False}}

    refs = list(ctx.sources)
    if len(refs) > 1:
        # Checking sources serially makes the health pill as slow as the sum of
        # every warehouse handshake, including the dead ones' timeouts.
        with ThreadPoolExecutor(max_workers=min(4, len(refs))) as pool:
            result["sources"] = list(pool.map(lambda r: _check_source(ctx, r), refs))
    else:
        result["sources"] = [_check_source(ctx, r) for r in refs]

    if settings.has_openai:
        try:
            reply = llm_client.ping(ctx)
            result["openai"] = {"ok": True, "model": settings.openai_model, "reply": reply}
        except Exception as exc:  # noqa: BLE001
            result["openai"] = {"ok": False, "error": str(exc)}
    else:
        result["openai"] = {"ok": False, "error": "OPENAI_API_KEY not set"}
    return result


@api.get("/schema")
def schema(refresh: bool = False, rctx: RequestContext = Depends(require_connection)) -> dict[str, Any]:
    try:
        context = build_catalog(rctx.tenant, force_refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return {"schema_context": context}


# --- report generation (streaming NDJSON) ---

# Shared with routes_datacontext, so the two stream the same event encoding.
_ndjson = ndjson
_drain = drain


@api.post("/report")
def report(req: ReportRequest, rctx: RequestContext = Depends(require_connection)) -> StreamingResponse:
    if not req.request.strip():
        raise HTTPException(status_code=400, detail="Empty request.")

    # Bound here, on the request thread, and captured by the worker closure
    # below. contextvars do NOT propagate into threading.Thread, so the tenant
    # must be passed explicitly or the worker would resolve the wrong org.
    ctx = rctx.tenant

    def fetch_suggestions() -> list[str]:
        # Deferred into the run (it overlaps build_catalog there) so the
        # embeddings round trip no longer delays the first response byte.
        # A FRESH session: this runs on a worker-side thread, and the
        # request-scoped session belongs to the request thread.
        if not req.use_memory:
            return []
        with session_scope() as db:
            return MemoryStore(db, ctx=ctx).retrieve_suggestion_texts(req.request, k=5)

    def stream():
        # Every DB read this endpoint needs happened in the handler body, so
        # give the pooled connection back before the multi-minute run.
        rctx.release_db()
        q: queue.Queue = queue.Queue()
        holder: dict[str, Any] = {}

        def on_event(kind: str, data: dict[str, Any]) -> None:
            q.put((kind, data))

        def worker() -> None:
            try:
                holder["result"] = generate_report(
                    req.request,
                    ctx=ctx,
                    memory_suggestions_fn=fetch_suggestions,
                    on_event=on_event,
                )
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc)
            finally:
                q.put(None)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        for kind, data in _drain(q):
            yield _ndjson(kind, data)

        t.join()
        if "result" in holder:
            res = holder["result"]
            # A FRESH session, on this thread. The request-scoped one belongs to
            # a different thread and was opened minutes ago: Starlette iterates a
            # sync generator via anyio.to_thread per item, so consecutive yields
            # can land on different threads, and a SQLAlchemy Session is not
            # thread-safe. Holding the request session open for the whole
            # generation would also pin a pooled connection for minutes.
            with session_scope() as db:
                saved = MemoryStore(db, ctx=ctx).save_report(
                    req.request, res.document, res.queries
                )
            yield _ndjson(
                "saved",
                {"report_id": saved.id, "queries": res.queries, "steps": res.steps},
            )
        elif "error" in holder:
            yield _ndjson("error", {"message": holder["error"]})
        yield _ndjson("done", {})

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# --- dashboards (streaming NDJSON) ---

@api.post("/dashboard")
def dashboard(req: DashboardRequest, rctx: RequestContext = Depends(require_connection)) -> StreamingResponse:
    if not req.request.strip():
        raise HTTPException(status_code=400, detail="Empty request.")

    ctx = rctx.tenant  # see /api/report: explicit, never a contextvar

    def fetch_suggestions() -> list[str]:
        # See /api/report: deferred into the run, fresh session, best-effort.
        if not req.use_memory:
            return []
        with session_scope() as db:
            return MemoryStore(db, ctx=ctx).retrieve_suggestion_texts(req.request, k=5)

    def stream():
        rctx.release_db()  # see /api/report: reads done, unpin the connection
        q: queue.Queue = queue.Queue()
        holder: dict[str, Any] = {}

        def on_event(kind: str, data: dict[str, Any]) -> None:
            q.put((kind, data))

        def worker() -> None:
            try:
                holder["result"] = generate_dashboard(
                    req.request,
                    ctx=ctx,
                    memory_suggestions_fn=fetch_suggestions,
                    on_event=on_event,
                )
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc)
            finally:
                q.put(None)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        for kind, data in _drain(q):
            yield _ndjson(kind, data)

        t.join()
        if "result" in holder:
            res = holder["result"]
            # A FRESH session, on this thread. The request-scoped one belongs to
            # a different thread and was opened minutes ago: Starlette iterates a
            # sync generator via anyio.to_thread per item, so consecutive yields
            # can land on different threads, and a SQLAlchemy Session is not
            # thread-safe. Holding the request session open for the whole
            # generation would also pin a pooled connection for minutes.
            with session_scope() as db:
                saved = MemoryStore(db, ctx=ctx).save_dashboard(
                    req.request,
                    res.document,
                    res.queries,
                    insights=res.insights,
                    authoring_document=res.authoring_document,
                )
            yield _ndjson(
                "saved",
                {"dashboard_id": saved.id, "queries": res.queries, "steps": res.steps},
            )
        elif "error" in holder:
            yield _ndjson("error", {"message": holder["error"]})
        yield _ndjson("done", {})

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@api.post("/dashboards/{dashboard_id}/analyze")
def analyze_dashboard_endpoint(
    dashboard_id: int, req: DashboardAnalyzeRequest, rctx: RequestContext = Depends(get_request_ctx)
) -> dict[str, Any]:
    # No require_connection: reads only the numbers already on the dashboard.
    store = rctx.store
    ctx = rctx.tenant
    saved = store.get_dashboard(dashboard_id)
    if not saved:
        raise HTTPException(status_code=404, detail="Dashboard not found.")
    suggestions: list[str] = []
    if req.use_memory:
        try:
            suggestions = store.retrieve_suggestion_texts(saved.request[:2000], k=3)
        except Exception:  # noqa: BLE001 - memory is best-effort
            suggestions = []
    try:
        analysis = analyze_dashboard(
            saved.document,
            ctx=ctx,
            focus=req.focus,
            memory_suggestions=suggestions,
            queries=saved.queries,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    store.set_dashboard_analysis(dashboard_id, analysis)
    return {"analysis": analysis}


@api.post("/dashboards/{dashboard_id}/refresh")
def refresh_dashboard_endpoint(
    dashboard_id: int,
    req: DashboardRefreshRequest,
    rctx: RequestContext = Depends(require_connection),
) -> Response:
    """Re-run this dashboard's captured queries and return a fresh document.

    Plain JSON rather than NDJSON: there is no LLM in this path, the queries run
    concurrently and are each capped by their source's timeout, and -- decisively
    -- a partially-arrived set of datasets is unrenderable. Materialization is
    server-side by the project's core trust rule, so streaming per-query results
    would force the frontend to reimplement ``materialize`` in TypeScript and
    become a second, drifting source of truth for every number on screen.
    """
    saved = rctx.store.get_dashboard(dashboard_id)
    if not saved:
        # Cross-org ids land here too (the store's read is org-scoped), so a 404
        # never confirms that another workspace has a dashboard with this id.
        raise HTTPException(status_code=404, detail="dashboard_not_found")

    ctx = rctx.tenant  # bound on the request thread, as everywhere else
    org_id = ctx.org_id

    if not refresh_svc.try_acquire(org_id, dashboard_id):
        raise HTTPException(status_code=409, detail="refresh_in_progress")
    try:
        # The warehouse round trip can take tens of seconds; holding the pooled
        # Postgres connection across it is exactly the pinning the streaming
        # endpoints above go out of their way to avoid.
        rctx.release_db()
        result = refresh_svc.refresh_dashboard(
            saved, ctx=ctx, selections=req.filters
        )
    except FilterError as exc:
        # A bad selection is the caller's, so it is a 400 with a stable code --
        # and it is raised before any SQL runs.
        raise HTTPException(status_code=400, detail=exc.code) from exc
    finally:
        refresh_svc.release(org_id, dashboard_id)

    payload = {
        "dashboard_id": dashboard_id,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "document": result.document.to_dict(),
        "datasets": [d.to_dict() for d in result.datasets],
        "partial": result.partial,
        "exact": result.exact,
        "frozen_stats": result.frozen_stats,
        "unfiltered": result.unfiltered,
        "unwired": result.unwired,
        # Echoed back so the client can see what the server actually honoured
        # and mark a filter whose definition has drifted.
        "applied_filters": req.filters,
    }
    # jsonsafe, not FastAPI's default encoder. This document is never persisted,
    # so unlike every other document on the wire it has passed through neither
    # the JSONB serializer nor ndjson(). `json.dumps` defaults to
    # allow_nan=True, and one NaN cell -- avg() over an empty group, routine once
    # a filter narrows a window to nothing -- would emit a bare `NaN` token and
    # make the browser reject the entire body.
    return Response(
        content=jsonsafe.dumps(payload), media_type="application/json"
    )


@api.put("/dashboards/{dashboard_id}/filters")
def set_dashboard_filters_endpoint(
    dashboard_id: int,
    req: DashboardFiltersRequest,
    rctx: RequestContext = Depends(require_connection),
) -> dict[str, Any]:
    """Define this dashboard's filters, rewriting its queries to accept them.

    PUT rather than PATCH: `allow_methods` on the CORS middleware above does not
    include PATCH, so a PATCH route would fail preflight with a browser-only
    error. Not `require_admin` either -- filters are dashboard content, not
    workspace configuration, so whoever may create a dashboard may filter one.
    """
    saved = rctx.store.get_dashboard(dashboard_id)
    if not saved:
        raise HTTPException(status_code=404, detail="dashboard_not_found")
    if saved.template:
        # A template's controls are part of the template: its SQL is written
        # against them, and a rewrite pass would replace hand-written templates
        # with model-written ones. Per-widget wiring is edited separately.
        raise HTTPException(status_code=409, detail="template_filters_fixed")

    ctx = rctx.tenant
    org_id = ctx.org_id
    defs = [d.model_dump(exclude_none=True) for d in req.filters]

    if not configure_svc.try_acquire(org_id, dashboard_id):
        raise HTTPException(status_code=409, detail="filters_configuring")
    try:
        rctx.release_db()  # the rewrite pass runs an LLM call plus N queries
        filters, report = configure_svc.configure_filters(saved, defs, ctx=ctx)
    finally:
        configure_svc.release(org_id, dashboard_id)

    if defs and not report.wired:
        # Every dataset refused the rewrite. Persisting the definitions would
        # leave controls on screen that cannot move a single number.
        raise HTTPException(status_code=409, detail="filter_rewrite_failed")

    with session_scope() as db:
        MemoryStore(db, ctx=ctx).set_dashboard_filters(dashboard_id, filters)

    return {
        "filters": filters,
        "wired": report.wired,
        # Named, not hidden: a filter that reaches 3 of 5 widgets has to say so.
        "skipped": report.skipped,
    }


@api.get("/dashboard-templates")
def list_dashboard_templates(
    rctx: RequestContext = Depends(get_request_ctx),
) -> dict[str, Any]:
    """Report templates this org can build, each with the sources it can use.

    Postgres-only (no warehouse is touched), so it does not depend on
    ``require_connection``: an org without a Jira source simply gets none.
    """
    refs = rctx.tenant.sources
    out = []
    for t in templates_svc.templates_for({r.type for r in refs}):
        out.append(
            {**t.summary(), "sources": [r.name for r in refs if r.type in t.source_types]}
        )
    return {"templates": out}


@api.post("/dashboards/from-template")
def create_dashboard_from_template(
    req: DashboardFromTemplateRequest,
    rctx: RequestContext = Depends(require_connection),
) -> Response:
    """Build a dashboard from a report template: no LLM, just its SQL.

    The first document comes from the same refresh path every later view
    uses, with each filter at its default.
    """
    template = templates_svc.get(req.template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="template_not_found")
    ctx = rctx.tenant
    try:
        inst = instantiate_svc.build(template, req.source, ctx)
    except instantiate_svc.TemplateSourceError:
        # Unknown and wrong-type sources are one answer; see resolve_source.
        raise HTTPException(status_code=404, detail="source_not_found") from None
    except instantiate_svc.SourceNeedsSyncError:
        raise HTTPException(status_code=409, detail="source_needs_sync") from None

    rctx.release_db()  # option probes done; now one query per dataset
    result = instantiate_svc.execute(inst, ctx)
    title = (req.title or "").strip() or inst.title

    with session_scope() as db:
        saved = MemoryStore(db, ctx=ctx).save_dashboard(
            request=f"{inst.title} ({req.source})",
            document=inst.document,
            queries=inst.queries,
            title=title,
            authoring_document=inst.authoring_document,
            filters=inst.filters,
            template=inst.template,
        )
        dashboard_id = saved.id

    return Response(
        content=jsonsafe.dumps(
            {
                "dashboard_id": dashboard_id,
                "title": title,
                "partial": result.partial,
                "datasets": [d.to_dict() for d in result.datasets],
            }
        ),
        media_type="application/json",
    )


def _editable_dashboard(rctx: RequestContext, dashboard_id: int):
    saved = rctx.store.get_dashboard(dashboard_id)
    if not saved:
        raise HTTPException(status_code=404, detail="dashboard_not_found")
    if not saved.is_refreshable:
        # Saved before the authoring document was kept: there is nothing that
        # says which column a stat tile read, so there is nothing to edit.
        raise HTTPException(status_code=409, detail="not_editable")
    return saved


@api.put("/dashboards/{dashboard_id}/layout")
def set_dashboard_layout(
    dashboard_id: int,
    req: DashboardLayoutRequest,
    rctx: RequestContext = Depends(get_request_ctx),
) -> dict[str, Any]:
    """Save an edited arrangement of this dashboard's existing widgets.

    Postgres-only: the client sees the result through its next refresh, the
    same way it sees a filter change.
    """
    saved = _editable_dashboard(rctx, dashboard_id)
    try:
        doc = layout_svc.sanitize(req.authoring_document, saved.queries)
    except layout_svc.LayoutError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    rctx.store.update_dashboard_content(dashboard_id, authoring_document=doc)
    return {"authoring_document": doc.to_dict()}


@api.post("/dashboards/{dashboard_id}/widgets")
def add_dashboard_widget(
    dashboard_id: int,
    req: AddWidgetRequest,
    rctx: RequestContext = Depends(get_request_ctx),
) -> dict[str, Any]:
    """Append one of the dashboard's template's catalog widgets."""
    saved = _editable_dashboard(rctx, dashboard_id)
    template = templates_svc.get((saved.template or {}).get("id", ""))
    if template is None:
        raise HTTPException(status_code=409, detail="not_a_template")
    try:
        queries, filters, authoring = instantiate_svc.add_widget(
            saved, template, req.widget_key
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="widget_not_found") from None
    rctx.store.update_dashboard_content(
        dashboard_id, queries=queries, filters=filters, authoring_document=authoring
    )
    return {"dataset_id": queries[-1]["dataset_id"], "authoring_document": authoring.to_dict()}


@api.post("/dashboards/{dashboard_id}/widgets/ai")
def add_ai_widget(
    dashboard_id: int,
    req: AIWidgetRequest,
    rctx: RequestContext = Depends(require_connection),
) -> StreamingResponse:
    """Ask for one more widget, generated by the Analyst and author agents.

    Streams like ``/api/dashboard``. The new datasets are not wired to the
    dashboard's filters -- a model-written query has no template -- and every
    refresh names them in ``unfiltered`` rather than letting them look filtered.
    """
    if not req.request.strip():
        raise HTTPException(status_code=400, detail="Empty request.")
    saved = _editable_dashboard(rctx, dashboard_id)
    taken = {str(q.get("dataset_id")) for q in saved.queries}
    ctx = rctx.tenant

    def stream():
        rctx.release_db()
        q: queue.Queue = queue.Queue()
        holder: dict[str, Any] = {}

        def worker() -> None:
            try:
                holder["result"] = generate_widget(
                    req.request, ctx=ctx, taken_ids=taken,
                    on_event=lambda kind, data: q.put((kind, data)),
                )
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc)
            finally:
                q.put(None)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        for kind, data in _drain(q):
            yield _ndjson(kind, data)
        t.join()

        res = holder.get("result")
        if res is not None and res.blocks:
            # Re-read: the generation took minutes, and the layout may have been
            # edited meanwhile. Append to what is stored now, not to `saved`.
            with session_scope() as db:
                store = MemoryStore(db, ctx=ctx)
                current = store.get_dashboard(dashboard_id)
                if current is None:
                    yield _ndjson("error", {"message": "The dashboard was deleted."})
                else:
                    doc = current.authoring_document.to_dict()
                    doc.setdefault("blocks", []).append({"type": "row", "children": res.blocks})
                    store.update_dashboard_content(
                        dashboard_id,
                        queries=[*current.queries, *res.queries],
                        authoring_document=Document.from_dict(doc),
                    )
                    yield _ndjson(
                        "widget",
                        {"dataset_ids": [x["dataset_id"] for x in res.queries],
                         "queries": res.queries},
                    )
        elif res is not None:
            yield _ndjson("error", {"message": res.reason})
        elif "error" in holder:
            yield _ndjson("error", {"message": holder["error"]})
        yield _ndjson("done", {})

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@api.put("/dashboards/{dashboard_id}/widgets/{dataset_id}/filters")
def set_widget_filters(
    dashboard_id: int,
    dataset_id: str,
    req: WidgetFiltersRequest,
    rctx: RequestContext = Depends(get_request_ctx),
) -> dict[str, Any]:
    """Choose which dashboard filters reach one dataset, and pin any of them.

    Only filters the dataset's SQL template can bind are accepted: wiring a
    filter a query cannot express would put a control on screen that silently
    moves nothing.
    """
    saved = _editable_dashboard(rctx, dashboard_id)
    filters = dict(saved.filters or {})
    templates = dict(filters.get("templates") or {})
    template = templates.get(dataset_id)
    if not isinstance(template, dict):
        raise HTTPException(status_code=404, detail="dataset_not_filterable")

    supported = layout_svc.dataset_filter_support(template)
    defs = {d["id"]: d for d in filters.get("filters") or [] if isinstance(d, dict)}
    wired = template.get("filters") if req.wired is None else req.wired
    if wired is not None and not set(wired) <= supported:
        raise HTTPException(status_code=400, detail="filter_unknown")
    if not set(req.overrides) <= (supported & set(defs)):
        raise HTTPException(status_code=400, detail="filter_unknown")
    try:
        # Same coercion and allowlist as a viewer's selection, before storing.
        for fid, selection in req.overrides.items():
            coerce_values({"filters": [defs[fid]]}, {fid: selection})
    except FilterError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc

    updated = {**template, "overrides": req.overrides}
    if wired is not None:
        order = list(defs)
        updated["filters"] = sorted(
            set(wired), key=lambda f: order.index(f) if f in order else len(order)
        )
    if not req.overrides:
        updated.pop("overrides")
    templates[dataset_id] = updated
    filters["templates"] = templates
    rctx.store.update_dashboard_content(dashboard_id, filters=filters)
    return {"dataset_id": dataset_id, "template": updated, "supported": sorted(supported)}


@api.get("/dashboards")
def list_dashboards(rctx: RequestContext = Depends(get_request_ctx)) -> dict[str, Any]:
    items = rctx.store.list_dashboards()
    return {
        "dashboards": [
            {"id": d.id, "request": d.request, "title": d.title, "created_at": d.created_at}
            for d in items
        ]
    }


@api.get("/dashboards/{dashboard_id}")
def get_dashboard(dashboard_id: int, rctx: RequestContext = Depends(get_request_ctx)) -> dict[str, Any]:
    d = rctx.store.get_dashboard(dashboard_id)
    if not d:
        raise HTTPException(status_code=404, detail="Dashboard not found.")
    return {
        "id": d.id,
        "request": d.request,
        "title": d.title,
        "document": d.document.to_dict(),
        "queries": d.queries,
        "insights": d.insights,
        "analysis": d.analysis,
        "created_at": d.created_at,
        # Whether a refresh can replay this dashboard exactly. False for rows
        # saved before the authoring document was kept: those still refresh
        # best-effort, but their stat tiles stay frozen, and the UI says so
        # rather than silently showing a mix of fresh and stale numbers.
        "refreshable": d.is_refreshable,
        "filters": d.filters,
        "template": d.template,
        # What the layout editor edits. Empty for a dashboard saved before the
        # authoring document was kept, which is exactly the "not editable" case.
        "authoring_document": d.authoring_document.to_dict(),
        "editable": d.is_refreshable,
        "widget_catalog": _widget_catalog(d.template),
    }


def _widget_catalog(template_ref: dict[str, Any]) -> list[dict[str, str]]:
    """What "Add widget" offers on a template dashboard; [] otherwise."""
    template = templates_svc.get((template_ref or {}).get("id", ""))
    if template is None:
        return []
    return [
        {"key": w.key, "title": w.title, "description": w.description}
        for w in template.all_widgets()
    ]


# --- Q&A about a report (streaming NDJSON) ---

@api.post("/reports/{report_id}/ask")
def ask(report_id: int, req: AskRequest, rctx: RequestContext = Depends(require_connection)) -> StreamingResponse:
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Empty question.")

    store = rctx.store
    ctx = rctx.tenant  # see /api/report: explicit, never a contextvar
    saved = store.get_report(report_id)
    if not saved:
        raise HTTPException(status_code=404, detail="Report not found.")

    # Prior turns become conversation context (answers flattened to text).
    from datatalk.agent.blocks import document_to_text

    conversation = [
        {"question": t.question, "answer": document_to_text(t.answer_document)}
        for t in store.list_qa_turns(report_id)
    ]

    def stream():
        rctx.release_db()  # see /api/report: reads done, unpin the connection
        q: queue.Queue = queue.Queue()
        holder: dict[str, Any] = {}

        def on_event(kind: str, data: dict[str, Any]) -> None:
            q.put((kind, data))

        def worker() -> None:
            try:
                holder["result"] = answer_question(
                    req.question,
                    ctx=ctx,
                    report_document=saved.document,
                    prior_queries=saved.queries,
                    conversation=conversation,
                    # Kept inside the worker: introspection can take seconds and
                    # hoisting it would delay the first NDJSON byte, hiding the
                    # "Querying (step 1)…" status the user relies on.
                    schema_context=build_catalog(ctx),
                    on_event=on_event,
                    report_id=report_id,
                )
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc)
            finally:
                q.put(None)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        for kind, data in _drain(q):
            yield _ndjson(kind, data)

        t.join()
        if "result" in holder:
            res = holder["result"]
            # A FRESH session, on this thread. The request-scoped one belongs to
            # a different thread and was opened minutes ago: Starlette iterates a
            # sync generator via anyio.to_thread per item, so consecutive yields
            # can land on different threads, and a SQLAlchemy Session is not
            # thread-safe. Holding the request session open for the whole
            # generation would also pin a pooled connection for minutes.
            with session_scope() as db:
                turn = MemoryStore(db, ctx=ctx).add_qa_turn(
                    report_id, req.question, res.answer_document, res.queries
                )
            yield _ndjson(
                "answer",
                {
                    "turn_id": turn.id,
                    "document": res.answer_document.to_dict(),
                    "queries": res.queries,
                },
            )
        elif "error" in holder:
            yield _ndjson("error", {"message": holder["error"]})
        yield _ndjson("done", {})

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# --- analysis ---

@api.post("/analyze")
def analyze(req: AnalyzeRequest, rctx: RequestContext = Depends(get_request_ctx)) -> dict[str, Any]:
    # No require_connection: this runs zero SQL (it critiques text it is
    # given), so a connectionless org can still analyze a pasted report —
    # matching the rule that only warehouse-reaching endpoints gate on one.
    store = rctx.store
    ctx = rctx.tenant
    queries = None
    if req.report_id is not None:
        saved = store.get_report(req.report_id)
        if not saved:
            raise HTTPException(status_code=404, detail="Report not found.")
        text, source, queries = saved.markdown, "own", saved.queries
    elif req.text and req.text.strip():
        text, source = req.text, "external"
    else:
        raise HTTPException(status_code=400, detail="Provide report_id or text.")

    suggestions: list[str] = []
    if req.use_memory:
        try:
            suggestions = store.retrieve_suggestion_texts(text[:2000], k=3)
        except Exception:  # noqa: BLE001 - memory is best-effort
            suggestions = []
    try:
        analysis = analyze_report(
            text,
            ctx=ctx,
            source=source,
            focus=req.focus,
            memory_suggestions=suggestions,
            queries=queries,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return {"analysis": analysis, "source": source}


# --- memory / feedback ("training") ---

@api.post("/feedback")
def add_feedback(req: FeedbackRequest, rctx: RequestContext = Depends(get_request_ctx)) -> dict[str, Any]:
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Empty suggestion.")
    try:
        s = rctx.store.add_suggestion(req.text)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return {"id": s.id, "text": s.text, "created_at": s.created_at}


@api.get("/suggestions")
def list_suggestions(rctx: RequestContext = Depends(get_request_ctx)) -> dict[str, Any]:
    items = rctx.store.all_suggestions()
    return {"suggestions": [{"id": s.id, "text": s.text, "created_at": s.created_at} for s in items]}


@api.delete("/suggestions/{suggestion_id}")
def delete_suggestion(
    suggestion_id: int, rctx: RequestContext = Depends(get_request_ctx)
) -> dict[str, Any]:
    # 404, not 403: a 403 would confirm the id exists in some other org.
    if not rctx.store.delete_suggestion(suggestion_id):
        raise HTTPException(status_code=404, detail="Suggestion not found.")
    return {"deleted": suggestion_id}


@api.get("/reports")
def list_reports(rctx: RequestContext = Depends(get_request_ctx)) -> dict[str, Any]:
    items = rctx.store.list_reports()
    return {
        "reports": [
            {"id": r.id, "request": r.request, "created_at": r.created_at} for r in items
        ]
    }


@api.get("/reports/{report_id}")
def get_report(report_id: int, rctx: RequestContext = Depends(get_request_ctx)) -> dict[str, Any]:
    store = rctx.store
    r = store.get_report(report_id)
    if not r:
        raise HTTPException(status_code=404, detail="Report not found.")
    turns = store.list_qa_turns(report_id)
    return {
        "id": r.id,
        "request": r.request,
        "markdown": r.markdown,
        "document": r.document.to_dict(),
        "queries": r.queries,
        "created_at": r.created_at,
        "qa_turns": [
            {
                "id": t.id,
                "question": t.question,
                "document": t.answer_document.to_dict(),
                "queries": t.queries,
                "created_at": t.created_at,
            }
            for t in turns
        ],
    }


# Routers are included last so every endpoint above is registered on `api`
# before it is attached to the app.
app.include_router(routes_auth.router)   # public: signup / login / logout / me
app.include_router(routes_orgs.router)   # protected via its own dependencies
app.include_router(routes_datacontext.router)  # ditto
app.include_router(api)

"""FastAPI web layer (Milestone 6).

Thin wrapper over the agent/memory packages. Report generation streams progress
as newline-delimited JSON (NDJSON) so the UI can show queries as they run.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from datatalk import clients
from datatalk.agent.analyze import analyze_dashboard, analyze_report
from datatalk.agent.dashboard import generate_dashboard
from datatalk.agent.qa import answer_question
from datatalk.agent.report import generate_report
from datatalk.auth import sessions as sessions_svc
from datatalk.config import get_settings
from datatalk.db import clickhouse, introspect
from datatalk.db.introspect import get_schema_context
from datatalk.db.session import get_engine, session_scope
from datatalk.llm import client as llm_client
from datatalk.memory.store import MemoryStore
from datatalk.web import routes_auth, routes_orgs
from datatalk.web.deps import RequestContext, csrf_guard, get_current_user, get_request_ctx

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Fail fast on a missing or out-of-date database, not on first request."""
    from datatalk.scripts.db import alembic_config, schema_is_current

    settings = get_settings()
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

    try:
        with session_scope() as db:
            swept = sessions_svc.sweep_expired(db)
        if swept:
            logger.info("Swept %d expired session(s).", swept)
    except Exception:  # noqa: BLE001 - never block startup on housekeeping
        logger.warning("Expired-session sweep failed.", exc_info=True)

    yield

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

_STATIC = Path(__file__).parent / "static"

app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

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


class FeedbackRequest(BaseModel):
    text: str


class AskRequest(BaseModel):
    question: str


# --- static ---

@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


# --- health / schema (connection checkout) ---

@api.get("/health")
def health(rctx: RequestContext = Depends(get_request_ctx)) -> dict[str, Any]:
    ctx = rctx.tenant
    settings = ctx.settings
    result: dict[str, Any] = {"clickhouse": {"ok": False}, "openai": {"ok": False}}
    try:
        info = clickhouse.ping(ctx.clickhouse)
        tables = introspect.introspect(ctx.clickhouse, ctx.settings, with_samples=False)
        result["clickhouse"] = {
            "ok": True,
            "version": info["version"],
            "database": info["database"],
            "table_count": len(tables),
        }
    except Exception as exc:  # noqa: BLE001
        result["clickhouse"] = {"ok": False, "error": str(exc)}

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
def schema(refresh: bool = False, rctx: RequestContext = Depends(get_request_ctx)) -> dict[str, Any]:
    try:
        context = introspect.get_schema_context(rctx.tenant, force_refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return {"schema_context": context}


# --- report generation (streaming NDJSON) ---

def _ndjson(kind: str, data: dict[str, Any]) -> str:
    # default=str: materialized rows can hold datetimes/Decimals from ClickHouse.
    return json.dumps({"kind": kind, "data": data}, default=str) + "\n"


@api.post("/report")
def report(req: ReportRequest, rctx: RequestContext = Depends(get_request_ctx)) -> StreamingResponse:
    if not req.request.strip():
        raise HTTPException(status_code=400, detail="Empty request.")

    store = rctx.store
    # Bound here, on the request thread, and captured by the worker closure
    # below. contextvars do NOT propagate into threading.Thread, so the tenant
    # must be passed explicitly or the worker would resolve the wrong org.
    ctx = rctx.tenant
    suggestions: list[str] = []
    if req.use_memory:
        try:
            suggestions = store.retrieve_suggestion_texts(req.request, k=5)
        except Exception:  # noqa: BLE001 - memory is best-effort
            suggestions = []

    def stream():
        q: queue.Queue = queue.Queue()
        holder: dict[str, Any] = {}

        def on_event(kind: str, data: dict[str, Any]) -> None:
            q.put((kind, data))

        def worker() -> None:
            try:
                holder["result"] = generate_report(
                    req.request,
                    ctx=ctx,
                    memory_suggestions=suggestions,
                    on_event=on_event,
                )
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc)
            finally:
                q.put(None)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        if suggestions:
            yield _ndjson("memory", {"count": len(suggestions), "suggestions": suggestions})

        while True:
            item = q.get()
            if item is None:
                break
            yield _ndjson(item[0], item[1])

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
def dashboard(req: DashboardRequest, rctx: RequestContext = Depends(get_request_ctx)) -> StreamingResponse:
    if not req.request.strip():
        raise HTTPException(status_code=400, detail="Empty request.")

    store = rctx.store
    ctx = rctx.tenant  # see /api/report: explicit, never a contextvar
    suggestions: list[str] = []
    if req.use_memory:
        try:
            suggestions = store.retrieve_suggestion_texts(req.request, k=5)
        except Exception:  # noqa: BLE001 - memory is best-effort
            suggestions = []

    def stream():
        q: queue.Queue = queue.Queue()
        holder: dict[str, Any] = {}

        def on_event(kind: str, data: dict[str, Any]) -> None:
            q.put((kind, data))

        def worker() -> None:
            try:
                holder["result"] = generate_dashboard(
                    req.request,
                    ctx=ctx,
                    memory_suggestions=suggestions,
                    on_event=on_event,
                )
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc)
            finally:
                q.put(None)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        if suggestions:
            yield _ndjson("memory", {"count": len(suggestions), "suggestions": suggestions})

        while True:
            item = q.get()
            if item is None:
                break
            yield _ndjson(item[0], item[1])

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
                    req.request, res.document, res.queries
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
    store = rctx.store
    ctx = rctx.tenant
    saved = store.get_dashboard(dashboard_id)
    if not saved:
        raise HTTPException(status_code=404, detail="Dashboard not found.")
    suggestions = (
        store.retrieve_suggestion_texts(saved.request[:2000], k=3)
        if req.use_memory else []
    )
    try:
        analysis = analyze_dashboard(
            saved.document, ctx=ctx, focus=req.focus, memory_suggestions=suggestions
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    store.set_dashboard_analysis(dashboard_id, analysis)
    return {"analysis": analysis}


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
        "analysis": d.analysis,
        "created_at": d.created_at,
    }


# --- Q&A about a report (streaming NDJSON) ---

@api.post("/reports/{report_id}/ask")
def ask(report_id: int, req: AskRequest, rctx: RequestContext = Depends(get_request_ctx)) -> StreamingResponse:
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
                    schema_context=get_schema_context(ctx),
                    on_event=on_event,
                )
            except Exception as exc:  # noqa: BLE001
                holder["error"] = str(exc)
            finally:
                q.put(None)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        while True:
            item = q.get()
            if item is None:
                break
            yield _ndjson(item[0], item[1])

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
    store = rctx.store
    ctx = rctx.tenant
    if req.report_id is not None:
        saved = store.get_report(req.report_id)
        if not saved:
            raise HTTPException(status_code=404, detail="Report not found.")
        text, source = saved.markdown, "own"
    elif req.text and req.text.strip():
        text, source = req.text, "external"
    else:
        raise HTTPException(status_code=400, detail="Provide report_id or text.")

    suggestions = store.retrieve_suggestion_texts(text[:2000], k=3) if req.use_memory else []
    try:
        analysis = analyze_report(
            text,
            ctx=ctx,
            source=source,
            focus=req.focus,
            memory_suggestions=suggestions,
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
app.include_router(api)

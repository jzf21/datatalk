"""FastAPI web layer (Milestone 6).

Thin wrapper over the agent/memory packages. Report generation streams progress
as newline-delimited JSON (NDJSON) so the UI can show queries as they run.
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from datatalk.agent.analyze import analyze_dashboard, analyze_report
from datatalk.agent.dashboard import generate_dashboard
from datatalk.agent.qa import answer_question
from datatalk.agent.report import generate_report
from datatalk.config import get_settings
from datatalk.db import clickhouse, introspect
from datatalk.db.introspect import get_schema_context
from datatalk.llm import client as llm_client
from datatalk.memory.store import MemoryStore

app = FastAPI(title="DataTalk", version="0.1.0")

_STATIC = Path(__file__).parent / "static"

app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


def _store() -> MemoryStore:
    # A fresh connection per request keeps SQLite thread-safe under the pool.
    return MemoryStore()


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

@app.get("/api/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    result: dict[str, Any] = {"clickhouse": {"ok": False}, "openai": {"ok": False}}
    try:
        info = clickhouse.ping()
        tables = introspect.introspect(with_samples=False)
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
            reply = llm_client.ping()
            result["openai"] = {"ok": True, "model": settings.openai_model, "reply": reply}
        except Exception as exc:  # noqa: BLE001
            result["openai"] = {"ok": False, "error": str(exc)}
    else:
        result["openai"] = {"ok": False, "error": "OPENAI_API_KEY not set"}
    return result


@app.get("/api/schema")
def schema(refresh: bool = False) -> dict[str, Any]:
    try:
        context = introspect.get_schema_context(force_refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return {"schema_context": context}


# --- report generation (streaming NDJSON) ---

def _ndjson(kind: str, data: dict[str, Any]) -> str:
    # default=str: materialized rows can hold datetimes/Decimals from ClickHouse.
    return json.dumps({"kind": kind, "data": data}, default=str) + "\n"


@app.post("/api/report")
def report(req: ReportRequest) -> StreamingResponse:
    if not req.request.strip():
        raise HTTPException(status_code=400, detail="Empty request.")

    store = _store()
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
                    req.request, memory_suggestions=suggestions, on_event=on_event
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
            saved = store.save_report(req.request, res.document, res.queries)
            yield _ndjson(
                "saved",
                {"report_id": saved.id, "queries": res.queries, "steps": res.steps},
            )
        elif "error" in holder:
            yield _ndjson("error", {"message": holder["error"]})
        yield _ndjson("done", {})

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# --- dashboards (streaming NDJSON) ---

@app.post("/api/dashboard")
def dashboard(req: DashboardRequest) -> StreamingResponse:
    if not req.request.strip():
        raise HTTPException(status_code=400, detail="Empty request.")

    store = _store()
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
                    req.request, memory_suggestions=suggestions, on_event=on_event
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
            saved = store.save_dashboard(req.request, res.document, res.queries)
            yield _ndjson(
                "saved",
                {"dashboard_id": saved.id, "queries": res.queries, "steps": res.steps},
            )
        elif "error" in holder:
            yield _ndjson("error", {"message": holder["error"]})
        yield _ndjson("done", {})

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/dashboards/{dashboard_id}/analyze")
def analyze_dashboard_endpoint(dashboard_id: int, req: DashboardAnalyzeRequest) -> dict[str, Any]:
    store = _store()
    saved = store.get_dashboard(dashboard_id)
    if not saved:
        raise HTTPException(status_code=404, detail="Dashboard not found.")
    suggestions = (
        store.retrieve_suggestion_texts(saved.request[:2000], k=3)
        if req.use_memory else []
    )
    try:
        analysis = analyze_dashboard(
            saved.document, focus=req.focus, memory_suggestions=suggestions
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    store.set_dashboard_analysis(dashboard_id, analysis)
    return {"analysis": analysis}


@app.get("/api/dashboards")
def list_dashboards() -> dict[str, Any]:
    items = _store().list_dashboards()
    return {
        "dashboards": [
            {"id": d.id, "request": d.request, "title": d.title, "created_at": d.created_at}
            for d in items
        ]
    }


@app.get("/api/dashboards/{dashboard_id}")
def get_dashboard(dashboard_id: int) -> dict[str, Any]:
    d = _store().get_dashboard(dashboard_id)
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

@app.post("/api/reports/{report_id}/ask")
def ask(report_id: int, req: AskRequest) -> StreamingResponse:
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Empty question.")

    store = _store()
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
                    report_document=saved.document,
                    prior_queries=saved.queries,
                    conversation=conversation,
                    schema_context=get_schema_context(),
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
            turn = store.add_qa_turn(
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

@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> dict[str, Any]:
    store = _store()
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
            text, source=source, focus=req.focus, memory_suggestions=suggestions
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return {"analysis": analysis, "source": source}


# --- memory / feedback ("training") ---

@app.post("/api/feedback")
def add_feedback(req: FeedbackRequest) -> dict[str, Any]:
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Empty suggestion.")
    try:
        s = _store().add_suggestion(req.text)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return {"id": s.id, "text": s.text, "created_at": s.created_at}


@app.get("/api/suggestions")
def list_suggestions() -> dict[str, Any]:
    items = _store().all_suggestions()
    return {"suggestions": [{"id": s.id, "text": s.text, "created_at": s.created_at} for s in items]}


@app.delete("/api/suggestions/{suggestion_id}")
def delete_suggestion(suggestion_id: int) -> dict[str, Any]:
    _store().delete_suggestion(suggestion_id)
    return {"deleted": suggestion_id}


@app.get("/api/reports")
def list_reports() -> dict[str, Any]:
    items = _store().list_reports()
    return {
        "reports": [
            {"id": r.id, "request": r.request, "created_at": r.created_at} for r in items
        ]
    }


@app.get("/api/reports/{report_id}")
def get_report(report_id: int) -> dict[str, Any]:
    store = _store()
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

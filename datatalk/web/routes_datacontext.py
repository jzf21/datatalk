"""The context model: per-file CRUD, and the streaming generation run.

File paths travel as a **query parameter**, never a path segment.
``ontology/customers.md`` contains a slash, so a path segment would need
encoding at every call site, would fight Starlette's route matching, and would
break the ``/openapi.json`` walk in ``tests/test_auth_web.py``.

Reads are member-visible: the context model reaches every prompt anyway and
holds no secrets. Everything that writes is admin-only, and generation
additionally requires a connection, because it spends real money on the docs
model.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from datatalk.agent import datacontext as agent_datacontext
from datatalk.db import models
from datatalk.db.session import session_scope
from datatalk.memory.datacontext import (
    MAX_BODY_CHARS,
    MAX_SUMMARY_CHARS,
    DataContextStore,
    FileDraft,
    SavedContextFile,
    invalidate_context,
    invalidate_context_on_commit,
    to_markdown,
)
from datatalk.web.deps import (
    RequestContext,
    csrf_guard,
    get_current_user,
    get_request_ctx,
    require_admin,
    require_connection,
    require_same_org,
)
from datatalk.web.streaming import MEDIA_TYPE, ndjson

# Router-level guards, so a route added here cannot forget either.
router = APIRouter(
    prefix="/api/orgs/{org_id}/context",
    tags=["context"],
    dependencies=[Depends(get_current_user), Depends(csrf_guard)],
)


class UpdateFileRequest(BaseModel):
    body_md: str = Field(max_length=MAX_BODY_CHARS)
    summary: str | None = Field(default=None, max_length=MAX_SUMMARY_CHARS)


class CoversEntry(BaseModel):
    source: str = Field(default="", max_length=40)
    table: str = Field(max_length=200)


class CreateFileRequest(BaseModel):
    path: str = Field(pattern=models.CONTEXT_PATH_RE, max_length=80)
    summary: str = Field(default="", max_length=MAX_SUMMARY_CHARS)
    body_md: str = Field(default="", max_length=MAX_BODY_CHARS)
    covers: list[CoversEntry] = Field(default_factory=list, max_length=40)


class GenerateRequest(BaseModel):
    # "revise" shows the model each current body and asks it to revise in place,
    # which is how a human's edits survive. "replace" starts over and therefore
    # needs confirm_overwrite.
    mode: Literal["revise", "replace"] = "revise"
    confirm_overwrite: bool = False
    keep_human_owned: bool = True
    max_tables_total: int | None = Field(default=None, ge=1, le=300)


def _file_summary(f: SavedContextFile) -> dict[str, Any]:
    """List shape: everything but the body, which can be 16 KB."""
    return {
        "path": f.path,
        "summary": f.summary,
        "origin": f.origin,
        "human_owned": f.human_owned,
        "bytes": len(f.body_md),
        "generated_at": f.generated_at,
        "edited_at": f.edited_at,
        "updated_at": f.updated_at,
    }


def _file_full(f: SavedContextFile) -> dict[str, Any]:
    return {**_file_summary(f), "body_md": f.body_md, "covers": f.covers, "evidence": f.evidence}


def _store(rctx: RequestContext, org_id: UUID) -> DataContextStore:
    require_same_org(rctx, org_id)
    return DataContextStore(rctx.db, ctx=rctx.tenant)


@router.get("")
def get_context(
    org_id: UUID, rctx: RequestContext = Depends(get_request_ctx)
) -> dict[str, Any]:
    """The tree, plus the exact bytes the tree contributes to every prompt.

    ``tree_preview`` is what makes the feature auditable: an operator can see
    literally what the agents are told, which is the whole thesis.
    """
    store = _store(rctx, org_id)
    doc = store.get_doc()
    return {
        "doc": None
        if doc is None
        else {"model": doc.model, "generated_at": doc.generated_at, "stats": doc.stats},
        "files": [_file_summary(f) for f in store.list_files()],
        "tree_preview": rctx.tenant.context_model.render_tree(),
    }


@router.get("/file")
def get_file(
    org_id: UUID,
    path: str = Query(..., max_length=80),
    rctx: RequestContext = Depends(get_request_ctx),
) -> dict[str, Any]:
    found = _store(rctx, org_id).get_file(path)
    if found is None:
        raise HTTPException(status_code=404, detail="context_file_not_found")
    return _file_full(found)


@router.put("/file")
def put_file(
    org_id: UUID,
    req: UpdateFileRequest,
    path: str = Query(..., max_length=80),
    rctx: RequestContext = Depends(get_request_ctx),
) -> dict[str, Any]:
    store = _store(rctx, org_id)
    require_admin(rctx)
    updated = store.update_file(path, body_md=req.body_md, summary=req.summary)
    if updated is None:
        raise HTTPException(status_code=404, detail="context_file_not_found")
    invalidate_context_on_commit(rctx.db, org_id)
    return _file_full(updated)


@router.post("/file", status_code=201)
def post_file(
    org_id: UUID,
    req: CreateFileRequest,
    rctx: RequestContext = Depends(get_request_ctx),
) -> dict[str, Any]:
    store = _store(rctx, org_id)
    require_admin(rctx)
    if store.get_file(req.path) is not None:
        raise HTTPException(status_code=409, detail="context_file_exists")
    try:
        created = store.upsert_file(
            FileDraft(
                path=req.path,
                summary=req.summary,
                body_md=req.body_md,
                covers=[c.model_dump() for c in req.covers],
            ),
            origin="human",
        )
    except ValueError:
        raise HTTPException(status_code=409, detail="context_file_limit") from None
    invalidate_context_on_commit(rctx.db, org_id)
    return _file_full(created)


@router.delete("/file", status_code=204)
def delete_file(
    org_id: UUID,
    path: str = Query(..., max_length=80),
    rctx: RequestContext = Depends(get_request_ctx),
) -> None:
    store = _store(rctx, org_id)
    require_admin(rctx)
    if not store.delete_file(path):
        raise HTTPException(status_code=404, detail="context_file_not_found")
    invalidate_context_on_commit(rctx.db, org_id)


@router.delete("", status_code=204)
def delete_context(
    org_id: UUID, rctx: RequestContext = Depends(get_request_ctx)
) -> None:
    store = _store(rctx, org_id)
    require_admin(rctx)
    store.clear()
    invalidate_context_on_commit(rctx.db, org_id)


@router.get("/export")
def export_context(
    org_id: UUID, rctx: RequestContext = Depends(get_request_ctx)
) -> dict[str, Any]:
    """The whole model as it would land on disk. The git-sync seam."""
    store = _store(rctx, org_id)
    require_admin(rctx)
    return {
        "files": [
            {"path": f.path, "markdown": to_markdown(f)} for f in store.list_files()
        ]
    }


@router.post("/generate")
def generate(
    org_id: UUID,
    req: GenerateRequest,
    rctx: RequestContext = Depends(require_connection),
) -> StreamingResponse:
    """Run the documentation agent, streaming NDJSON progress.

    Everything that can 4xx is raised *before* the StreamingResponse is
    returned: once bytes are flowing the only channel left is an ``error``
    event, and a 409 delivered that way would not stop the client acting as if
    the request had been accepted.

    The in-flight guard is per process. That covers the double-click, which is
    the case that actually happens; two uvicorn workers racing is not worth a
    database lock here.
    """
    require_same_org(rctx, org_id)
    require_admin(rctx)

    store = DataContextStore(rctx.db, ctx=rctx.tenant)
    existing = store.list_files()
    if existing and req.mode == "replace" and not req.confirm_overwrite:
        raise HTTPException(status_code=409, detail="context_exists")

    if not agent_datacontext.try_acquire(org_id):
        raise HTTPException(status_code=409, detail="context_generating")

    # Bound here, on the request thread, and captured by the worker closure:
    # contextvars do NOT propagate into threading.Thread, so the tenant must be
    # passed explicitly or the worker would resolve the wrong org.
    ctx = rctx.tenant
    kwargs: dict[str, Any] = {}
    if req.max_tables_total is not None:
        kwargs["max_tables_total"] = req.max_tables_total

    def stream():
        q: queue.Queue = queue.Queue()
        holder: dict[str, Any] = {}

        def on_event(kind: str, data: dict[str, Any]) -> None:
            q.put((kind, data))

        def worker() -> None:
            try:
                holder["result"] = agent_datacontext.generate_data_context(
                    ctx=ctx,
                    existing=existing,
                    mode=req.mode,
                    on_event=on_event,
                    **kwargs,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as an error event
                holder["error"] = str(exc)
            finally:
                agent_datacontext.release(org_id)
                q.put(None)  # sentinel

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        while True:
            item = q.get()
            if item is None:
                break
            yield ndjson(item[0], item[1])

        t.join()
        if "result" in holder:
            res = holder["result"]
            # A FRESH session, on this thread. The request-scoped one belongs to
            # a different thread and was opened minutes ago: Starlette iterates a
            # sync generator via anyio.to_thread per item, so consecutive yields
            # can land on different threads, and a Session is not thread-safe.
            with session_scope() as db:
                doc = DataContextStore(db, ctx=ctx).apply_generation(
                    res.files,
                    model=res.model,
                    stats=res.stats,
                    mode=req.mode,
                    keep_human_owned=req.keep_human_owned,
                )
            invalidate_context(org_id)  # after the commit, never before
            yield ndjson(
                "saved",
                {"files": len(res.files), "model": doc.model, "stats": doc.stats},
            )
        elif "error" in holder:
            yield ndjson("error", {"message": holder["error"]})
        yield ndjson("done", {})

    return StreamingResponse(stream(), media_type=MEDIA_TYPE)

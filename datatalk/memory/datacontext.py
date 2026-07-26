"""The org's context model: storage, the process cache, and git serialization.

The agents' real bottleneck is not the model, it is that the only semantic
metadata reaching a prompt is one free-text description per source. This module
owns the fix: a per-org tree of markdown files -- an entity ontology and analysis
playbooks -- whose one-line summaries go into every catalog-bearing prompt and
whose bodies are fetched on demand through the ``read_context`` tool.

**Why the cache exists.** ``read_context`` runs inside ``run_capture_loop`` on a
daemon worker thread, and ``build_catalog`` is likewise called from the Q&A
worker. Neither has a database session, and ``agent/`` and ``warehouse/`` are
deliberately session-free. So the whole model is loaded once, on the request
thread that does hold a session, and handed to the worker as a frozen
:class:`~datatalk.context.ContextModel`. :func:`load_context` amortizes that read
across requests; worker threads never call it.

The memory ceiling is bounded on purpose and the bounds are load-bearing, not
cosmetic: ``MAX_FILES`` files per org, ``MAX_BODY_CHARS`` per file (also enforced
as a Pydantic ``max_length`` at the API), and ``_MAX_ORGS`` cached -- about 32 MB
worst case, and ~60 KB for a realistic org.

Storage conventions follow :mod:`datatalk.memory.store` exactly: every read goes
through ``_scoped``, no ORM instance escapes, and the store never commits.

**The context model is a prompt-injection surface.** It is written by an LLM with
SQL access and edited by an admin, and it lands in every agent prompt. It cannot
cause a write -- ``agent/executor.py``'s guardrails are unchanged -- but it can
steer the Analyst toward the wrong table or an expensive query. The mitigations
are: its own fence with explicit "authoritative for meaning, not for what
exists" framing, ``SQL RULES`` and the anti-fabrication rule rendered *after*
it, hard length caps, and admin-only writes. Do not relax them.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import Select, delete, event, select
from sqlalchemy.orm import Session

from datatalk.context import (
    EMPTY_CONTEXT_MODEL,
    ContextFile,
    ContextModel,
    TenantContext,
)
from datatalk.db import models

# One file's markdown. Also the API's max_length, so an oversize body is a 422
# rather than something that quietly bloats every prompt.
MAX_BODY_CHARS = 16_000
MAX_SUMMARY_CHARS = 200
MAX_FILES = 60

_PATH_RE = re.compile(models.CONTEXT_PATH_RE)


def valid_path(path: str) -> bool:
    return bool(_PATH_RE.match(path or ""))


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- dataclasses out, never ORM ----------------------------------------------


@dataclass
class SavedContextFile:
    id: int
    path: str
    summary: str
    body_md: str
    generated_body_md: str | None
    covers: list[dict[str, str]]
    evidence: list[dict[str, Any]]
    origin: str
    generated_at: str
    edited_at: str
    created_at: str
    updated_at: str

    @property
    def human_owned(self) -> bool:
        """True when body_md holds words the generator did not write.

        ``generated_body_md`` is the merge base -- git's index, not a backup.
        After a revise run the agent has incorporated the human's edit, so both
        move forward together and the file stops being divergent.
        """
        return self.generated_body_md is None or self.body_md != self.generated_body_md


@dataclass
class SavedContextDoc:
    id: int
    model: str
    generated_at: str
    stats: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass
class FileDraft:
    """What the generation agent emits and the store writes."""

    path: str
    summary: str
    body_md: str
    covers: list[dict[str, str]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)


# --- store --------------------------------------------------------------------


class DataContextStore:
    """Org-scoped context-model storage. The caller owns the transaction.

    Kept out of :class:`~datatalk.memory.store.MemoryStore` on purpose: that
    class is about *generated content* and is instantiated on every request,
    while this is configuration, admin-guarded, and read through a process cache
    on a different lifecycle. They share no state.
    """

    def __init__(self, session: Session, *, ctx: TenantContext):
        self._db = session
        self.ctx = ctx
        self.org_id = ctx.org_id
        self.user_id = ctx.user_id

    def _scoped(self, model: type) -> Select:
        """The only way reads should enter this class."""
        return select(model).where(model.org_id == self.org_id)

    # --- reads ---

    def get_doc(self) -> SavedContextDoc | None:
        row = self._db.execute(
            self._scoped(models.DataContextDoc)
        ).scalar_one_or_none()
        return self._to_doc(row) if row else None

    def list_files(self) -> list[SavedContextFile]:
        rows = self._db.execute(
            self._scoped(models.DataContextFile).order_by(models.DataContextFile.path)
        ).scalars()
        return [self._to_file(r) for r in rows]

    def get_file(self, path: str) -> SavedContextFile | None:
        row = self._row(path)
        return self._to_file(row) if row else None

    def _row(self, path: str) -> models.DataContextFile | None:
        return self._db.execute(
            self._scoped(models.DataContextFile).where(
                models.DataContextFile.path == path
            )
        ).scalar_one_or_none()

    # --- writes ---

    def upsert_file(
        self,
        draft: FileDraft,
        *,
        origin: str = "human",
        generated: bool = False,
    ) -> SavedContextFile:
        """Create or overwrite one file.

        ``generated=True`` also advances ``generated_body_md`` to the new body,
        which is what makes a revised file stop reading as human-owned.
        """
        if not valid_path(draft.path):
            raise ValueError(f"Invalid context path {draft.path!r}")

        row = self._row(draft.path)
        if row is None:
            if self._count() >= MAX_FILES:
                raise ValueError(f"A workspace may hold at most {MAX_FILES} context files.")
            row = models.DataContextFile(
                org_id=self.org_id,
                created_by_user_id=self.user_id,
                path=draft.path,
                origin=origin,
            )
            self._db.add(row)

        row.summary = draft.summary[:MAX_SUMMARY_CHARS]
        row.body_md = draft.body_md[:MAX_BODY_CHARS]
        row.covers = list(draft.covers or [])
        if draft.evidence:
            row.evidence = list(draft.evidence)
        if generated:
            row.generated_body_md = row.body_md
            row.generated_at = _now()
        else:
            row.edited_at = _now()
            row.edited_by_user_id = self.user_id

        self._db.flush()
        return self._to_file(row)

    def update_file(
        self, path: str, *, body_md: str, summary: str | None = None
    ) -> SavedContextFile | None:
        """Save a hand edit. ``None`` for another org's path, which the endpoint
        turns into a 404 -- never a 403, never a confirmation it exists."""
        row = self._row(path)
        if row is None:
            return None
        row.body_md = body_md[:MAX_BODY_CHARS]
        if summary is not None:
            row.summary = summary[:MAX_SUMMARY_CHARS]
        row.edited_at = _now()
        row.edited_by_user_id = self.user_id
        self._db.flush()
        return self._to_file(row)

    def delete_file(self, path: str) -> bool:
        result = self._db.execute(
            delete(models.DataContextFile)
            .where(models.DataContextFile.path == path)
            .where(models.DataContextFile.org_id == self.org_id)
        )
        return bool(result.rowcount)

    def clear(self) -> bool:
        """Drop the whole context model for this org."""
        files = self._db.execute(
            delete(models.DataContextFile).where(
                models.DataContextFile.org_id == self.org_id
            )
        )
        self._db.execute(
            delete(models.DataContextDoc).where(
                models.DataContextDoc.org_id == self.org_id
            )
        )
        return bool(files.rowcount)

    def apply_generation(
        self,
        drafts: list[FileDraft],
        *,
        model: str,
        stats: dict[str, Any] | None = None,
        mode: str = "revise",
        keep_human_owned: bool = True,
    ) -> SavedContextDoc:
        """Write a generation run's output and stamp the run metadata.

        Per draft path:

        * a file whose ``origin`` is not ``agent`` is **skipped** -- a
          dbt/GitHub-owned file is never touched by the warehouse generator,
          whatever the prompt produced;
        * in ``replace`` mode with ``keep_human_owned``, a human-owned file is
          skipped too;
        * otherwise the body advances and ``generated_body_md`` advances with
          it. In ``revise`` mode the agent was *shown* the human's edit and told
          to fold it in, so the merge base moving forward is correct -- which is
          why revise needs no post-hoc merge step.

        ``replace`` additionally deletes agent-origin files the drafts did not
        name (a table that no longer exists loses its file). ``revise`` never
        deletes.
        """
        existing = {r.path: r for r in self.list_files()}
        written: set[str] = set()

        for draft in drafts:
            if not valid_path(draft.path):
                continue
            prior = existing.get(draft.path)
            if prior is not None:
                if prior.origin != "agent":
                    continue
                if mode == "replace" and keep_human_owned and prior.human_owned:
                    continue
            self.upsert_file(draft, origin="agent", generated=True)
            written.add(draft.path)

        if mode == "replace":
            for path, prior in existing.items():
                if path not in written and prior.origin == "agent":
                    self.delete_file(path)

        return self._upsert_doc(model=model, stats=stats or {})

    def _upsert_doc(self, *, model: str, stats: dict[str, Any]) -> SavedContextDoc:
        row = self._db.execute(
            self._scoped(models.DataContextDoc)
        ).scalar_one_or_none()
        if row is None:
            row = models.DataContextDoc(
                org_id=self.org_id, created_by_user_id=self.user_id
            )
            self._db.add(row)
        row.model = model
        row.stats = stats
        row.generated_at = _now()
        self._db.flush()
        return self._to_doc(row)

    def _count(self) -> int:
        return len(
            list(self._db.execute(self._scoped(models.DataContextFile)).scalars())
        )

    # --- row -> dataclass ---

    @staticmethod
    def _to_file(r: models.DataContextFile) -> SavedContextFile:
        return SavedContextFile(
            id=r.id,
            path=r.path,
            summary=r.summary or "",
            body_md=r.body_md or "",
            generated_body_md=r.generated_body_md,
            covers=list(r.covers or []),
            evidence=list(r.evidence or []),
            origin=r.origin or "agent",
            generated_at=_iso(r.generated_at),
            edited_at=_iso(r.edited_at),
            created_at=_iso(r.created_at),
            updated_at=_iso(r.updated_at),
        )

    @staticmethod
    def _to_doc(r: models.DataContextDoc) -> SavedContextDoc:
        return SavedContextDoc(
            id=r.id,
            model=r.model or "",
            generated_at=_iso(r.generated_at),
            stats=dict(r.stats or {}),
            created_at=_iso(r.created_at),
            updated_at=_iso(r.updated_at),
        )


# --- the process cache --------------------------------------------------------
#
# Same shape as warehouse/catalog._SCHEMA_CACHE: a dict, a lock, a TTL, and an
# explicit invalidator called from every mutation.


@dataclass
class _Cached:
    model: ContextModel
    built_at: float


_CACHE: dict[UUID, _Cached] = {}
_LOCK = threading.Lock()
_TTL = 300.0
# A failure is cached far more briefly than a success: a transient database
# error must not blind an org's agents for the full TTL.
_FAIL_TTL = 15.0
_MAX_ORGS = 32  # same ceiling, and same reasoning, as clients._MAX_ENTRIES


def _to_context_file(r: SavedContextFile) -> ContextFile:
    covers = tuple(
        (str(c.get("source", "")), str(c.get("table", "")))
        for c in (r.covers or [])
        if isinstance(c, dict) and c.get("table")
    )
    return ContextFile(
        id=r.id,
        path=r.path,
        summary=r.summary,
        body_md=r.body_md,
        origin=r.origin,
        covers=covers,
    )


def load_context(db: Session, org_id: UUID) -> ContextModel:
    """The frozen :class:`ContextModel` for an org, cached in process.

    Filled only from a request thread that already owns ``db``. Worker threads
    never call this -- they read the snapshot handed to them on the frozen
    ``TenantContext``, which is what keeps ``agent/`` and ``warehouse/``
    session-free.

    Never raises: a broken context model must degrade the prompts, not 500 the
    request.
    """
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(org_id)
        if hit is not None:
            ttl = _FAIL_TTL if hit.model.is_empty else _TTL
            if (now - hit.built_at) < ttl:
                return hit.model

    try:
        rows = db.execute(
            select(models.DataContextFile)
            .where(models.DataContextFile.org_id == org_id)
            .order_by(models.DataContextFile.path)
        ).scalars()
        model = ContextModel(
            files=tuple(
                _to_context_file(DataContextStore._to_file(r)) for r in rows
            )
        )
    except Exception:  # noqa: BLE001 - documentation must never break a request
        model = EMPTY_CONTEXT_MODEL

    with _LOCK:
        _CACHE[org_id] = _Cached(model, now)
        if len(_CACHE) > _MAX_ORGS:
            oldest = sorted(_CACHE.items(), key=lambda kv: kv[1].built_at)
            for key, _ in oldest[: len(_CACHE) - _MAX_ORGS]:
                _CACHE.pop(key, None)
    return model


def invalidate_context(org_id: UUID) -> None:
    """Forget an org's cached context model. Call after every write commits."""
    with _LOCK:
        _CACHE.pop(org_id, None)


def invalidate_context_on_commit(db: Session, org_id: UUID) -> None:
    """Drop the cached model once ``db``'s transaction actually commits.

    Ordering matters here in a way it does not for the schema cache. That one
    refills from the warehouse, so an early invalidation is harmless; this one
    refills from the very transaction being committed. Invalidating before the
    commit lets a concurrent request repopulate the cache with pre-commit rows,
    and the edit then looks lost for the whole TTL.

    Request handlers do not commit -- ``db_session`` does, at dependency
    teardown -- so this hangs a one-shot listener on the session rather than
    guessing when that happens. Sessions that never commit (the test harness
    rolls back instead) simply never fire it.
    """

    @event.listens_for(db, "after_commit", once=True)
    def _drop(_session: Session) -> None:  # pragma: no cover - trivial
        invalidate_context(org_id)


# --- git serialization --------------------------------------------------------
#
# Sync itself is not built, but the shape is fixed here so the storage model
# stays lossless and a future sync is a serializer rather than a schema change.

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)


def to_markdown(f: SavedContextFile) -> str:
    """The file as it would land on disk: frontmatter carries what columns hold.

    ``generated_body_md`` is deliberately not exported. It is a merge base --
    git's index -- and has no meaning in a working tree.
    """
    lines = ["---", f"summary: {f.summary}", f"origin: {f.origin}"]
    if f.covers:
        lines.append("covers:")
        for c in f.covers:
            lines.append(f"  - source: {c.get('source', '')}")
            lines.append(f"    table: {c.get('table', '')}")
    lines += ["---", "", f.body_md]
    return "\n".join(lines)


def from_markdown(path: str, text: str) -> FileDraft:
    """Inverse of :func:`to_markdown`. Bad frontmatter degrades to defaults."""
    summary, covers = "", []
    body = text

    match = _FRONTMATTER_RE.match(text or "")
    if match:
        body = text[match.end() :]
        pending: dict[str, str] = {}
        for raw in match.group(1).splitlines():
            line = raw.rstrip()
            if line.startswith("summary:"):
                summary = line.split(":", 1)[1].strip()
            elif line.strip().startswith("- source:"):
                if pending.get("table"):
                    covers.append(pending)
                pending = {"source": line.split(":", 1)[1].strip()}
            elif line.strip().startswith("table:"):
                pending["table"] = line.split(":", 1)[1].strip()
        if pending.get("table"):
            covers.append(pending)

    return FileDraft(
        path=path,
        summary=summary,
        body_md=body.lstrip("\n"),
        covers=covers,
    )

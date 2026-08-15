"""Org-scoped memory + report persistence (Postgres).

"Training from user suggestions" is retrieval-augmented prompting: suggestions
are stored with an embedding, and the most relevant ones are injected into the
prompt at generation time. No fine-tuning involved.

Two invariants this module exists to hold:

1. **Every read filters by org_id; every write stamps it.** Reads go through
   :meth:`MemoryStore._scoped`, so "does this query leak across tenants?" is
   answerable by grepping for ``select(`` outside that helper.

2. **No ORM instance ever escapes.** Every method returns a plain dataclass.
   The streaming endpoints in ``web/app.py`` read these from a *different*
   thread than the one that loaded them, minutes later; a live ORM object there
   would lazy-load against a closed session and raise DetachedInstanceError.

The store does not commit. Transaction boundaries belong to the caller
(:func:`datatalk.db.session.session_scope`), so several writes compose into one
transaction and a failure mid-stream leaves nothing half-persisted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy import Select, delete, select, update
from sqlalchemy.orm import Session

from datatalk.agent.blocks import Document, document_to_text
from datatalk.context import TenantContext
from datatalk.db import models
from datatalk.llm.client import embed


@dataclass
class Suggestion:
    id: int
    text: str
    created_at: str
    score: float = 0.0
    created_by_user_id: UUID | None = None


@dataclass
class SavedReport:
    id: int
    request: str
    markdown: str
    created_at: str
    document: Document = field(default_factory=Document)
    queries: list[dict[str, Any]] = field(default_factory=list)
    created_by_user_id: UUID | None = None


@dataclass
class QATurn:
    id: int
    report_id: int
    question: str
    answer_document: Document
    queries: list[dict[str, Any]]
    created_at: str
    created_by_user_id: UUID | None = None


@dataclass
class SavedDashboard:
    id: int
    request: str
    title: str
    created_at: str
    document: Document = field(default_factory=Document)
    queries: list[dict[str, Any]] = field(default_factory=list)
    insights: dict[str, Any] = field(default_factory=dict)
    analysis: str | None = None
    created_by_user_id: UUID | None = None
    # The pre-materialization document, replayed by a refresh. Empty for
    # dashboards saved before it was persisted -- see `is_refreshable`.
    authoring_document: Document = field(default_factory=Document)
    filters: dict[str, Any] = field(default_factory=dict)

    @property
    def is_refreshable(self) -> bool:
        """Whether this dashboard can be refreshed exactly.

        A real authoring document always has at least one block, so its emptiness
        is the predicate -- no separate flag column that could drift from it.
        False means "saved before the authoring document was kept": such a
        dashboard still refreshes best-effort via
        :func:`~datatalk.agent.blocks.dematerialize`, but its stat tiles stay
        frozen because the column each one read was not recorded.
        """
        return bool(self.authoring_document.blocks)


def _iso(value: datetime | None) -> str:
    """Timestamps cross the wire as ISO-8601 strings, as they always have."""
    return value.isoformat() if value is not None else ""


class CrossOrgError(PermissionError):
    """An operation referenced a resource belonging to another org."""


class MemoryStore:
    """Postgres-backed, org-scoped store.

    Construct one per unit of work around a session you own::

        with session_scope() as db:
            store = MemoryStore(db, ctx=ctx)
            store.save_report(...)
    """

    def __init__(self, session: Session, *, ctx: TenantContext):
        self._db = session
        self.ctx = ctx
        self.org_id = ctx.org_id
        self.user_id = ctx.user_id

    # --- scoping ---

    def _scoped(self, model: type) -> Select:
        """The only way reads should enter this class."""
        return select(model).where(model.org_id == self.org_id)

    def close(self) -> None:
        """No-op. Session lifetime belongs to the caller (session_scope)."""

    # --- suggestions (few-shot memory) ---

    def add_suggestion(self, text: str) -> Suggestion:
        text = (text or "").strip()
        if not text:
            raise ValueError("Suggestion text is empty.")

        vec = np.asarray(embed([text], self.ctx)[0], dtype=np.float32)
        row = models.Suggestion(
            org_id=self.org_id,
            created_by_user_id=self.user_id,
            text_=text,
            embedding=vec.tobytes(),
            embedding_dim=int(vec.shape[0]),
            embedding_model=self.ctx.settings.openai_embed_model,
        )
        self._db.add(row)
        self._db.flush()  # populate row.id without committing
        return Suggestion(
            id=row.id,
            text=text,
            created_at=_iso(row.created_at),
            created_by_user_id=row.created_by_user_id,
        )

    def all_suggestions(self) -> list[Suggestion]:
        rows = self._db.execute(
            self._scoped(models.Suggestion).order_by(models.Suggestion.id.desc())
        ).scalars()
        return [
            Suggestion(
                id=r.id,
                text=r.text_,
                created_at=_iso(r.created_at),
                created_by_user_id=r.created_by_user_id,
            )
            for r in rows
        ]

    def delete_suggestion(self, suggestion_id: int) -> bool:
        """Return whether a row was deleted.

        Returns False for another org's id rather than reporting success -- the
        endpoint turns that into a 404.
        """
        result = self._db.execute(
            delete(models.Suggestion)
            .where(models.Suggestion.id == suggestion_id)
            .where(models.Suggestion.org_id == self.org_id)
        )
        return bool(result.rowcount)

    def retrieve_suggestions(self, query: str, k: int = 5) -> list[Suggestion]:
        """Return up to ``k`` of this org's suggestions most similar to ``query``.

        Brute-force cosine in numpy. Suggestions are hand-written hints numbering
        in the tens per org, so an ANN index would add deployment friction to
        solve a problem that does not exist. ``memory_max_candidates`` bounds the
        scan so one org cannot grow it without limit.
        """
        model_name = self.ctx.settings.openai_embed_model
        q = np.asarray(embed([query], self.ctx)[0], dtype=np.float32)

        rows = self._db.execute(
            self._scoped(models.Suggestion)
            # Vectors from different models have different dimensions; mixing
            # them makes np.dot raise and (because the web layer swallows
            # retrieval errors) silently disables memory entirely.
            .where(models.Suggestion.embedding_dim == int(q.shape[0]))
            .where(models.Suggestion.embedding_model == model_name)
            .order_by(models.Suggestion.id.desc())
            .limit(self.ctx.settings.memory_max_candidates)
        ).scalars()

        q_norm = q / (np.linalg.norm(q) + 1e-8)
        scored: list[Suggestion] = []
        for r in rows:
            vec = np.frombuffer(r.embedding, dtype=np.float32)
            vec_norm = vec / (np.linalg.norm(vec) + 1e-8)
            scored.append(
                Suggestion(
                    id=r.id,
                    text=r.text_,
                    created_at=_iso(r.created_at),
                    score=float(np.dot(q_norm, vec_norm)),
                    created_by_user_id=r.created_by_user_id,
                )
            )
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[:k]

    def retrieve_suggestion_texts(self, query: str, k: int = 5) -> list[str]:
        return [s.text for s in self.retrieve_suggestions(query, k)]

    # --- reports ---

    def save_report(
        self,
        request: str,
        document: Document,
        queries: list[dict[str, Any]] | None = None,
    ) -> SavedReport:
        """Persist a report. ``markdown`` is derived from the Document so the
        Analyze agent and memory retrieval keep working unchanged."""
        queries = queries or []
        markdown = document_to_text(document)
        row = models.Report(
            org_id=self.org_id,
            created_by_user_id=self.user_id,
            request=request,
            markdown=markdown,
            document=document.to_dict(),
            queries=queries,
        )
        self._db.add(row)
        self._db.flush()
        return SavedReport(
            id=row.id,
            request=request,
            markdown=markdown,
            created_at=_iso(row.created_at),
            document=document,
            queries=queries,
            created_by_user_id=row.created_by_user_id,
        )

    @staticmethod
    def _to_report(r: models.Report) -> SavedReport:
        return SavedReport(
            id=r.id,
            request=r.request,
            markdown=r.markdown,
            created_at=_iso(r.created_at),
            document=Document.from_dict(r.document or {}),
            queries=list(r.queries or []),
            created_by_user_id=r.created_by_user_id,
        )

    def get_report(self, report_id: int) -> SavedReport | None:
        row = self._db.execute(
            self._scoped(models.Report).where(models.Report.id == report_id)
        ).scalar_one_or_none()
        return self._to_report(row) if row else None

    def list_reports(self, limit: int = 50) -> list[SavedReport]:
        rows = self._db.execute(
            self._scoped(models.Report).order_by(models.Report.id.desc()).limit(limit)
        ).scalars()
        return [self._to_report(r) for r in rows]

    # --- Q&A turns ---

    def add_qa_turn(
        self,
        report_id: int,
        question: str,
        answer_document: Document,
        queries: list[dict[str, Any]] | None = None,
    ) -> QATurn:
        # The composite FK (report_id, org_id) already makes a cross-org turn
        # impossible; checking first turns an IntegrityError into a clear error.
        if self.get_report(report_id) is None:
            raise CrossOrgError(f"Report {report_id} does not belong to this org.")

        queries = queries or []
        row = models.QATurn(
            org_id=self.org_id,
            report_id=report_id,
            created_by_user_id=self.user_id,
            question=question,
            answer_document=answer_document.to_dict(),
            queries=queries,
        )
        self._db.add(row)
        self._db.flush()
        return QATurn(
            id=row.id,
            report_id=report_id,
            question=question,
            answer_document=answer_document,
            queries=queries,
            created_at=_iso(row.created_at),
            created_by_user_id=row.created_by_user_id,
        )

    def list_qa_turns(self, report_id: int) -> list[QATurn]:
        rows = self._db.execute(
            self._scoped(models.QATurn)
            .where(models.QATurn.report_id == report_id)
            .order_by(models.QATurn.id.asc())
        ).scalars()
        return [
            QATurn(
                id=r.id,
                report_id=r.report_id,
                question=r.question,
                answer_document=Document.from_dict(r.answer_document or {}),
                queries=list(r.queries or []),
                created_at=_iso(r.created_at),
                created_by_user_id=r.created_by_user_id,
            )
            for r in rows
        ]

    # --- dashboards ---

    @staticmethod
    def _derive_title(request: str) -> str:
        title = (request or "").strip().splitlines()[0] if (request or "").strip() else ""
        return title[:80] or "Dashboard"

    def save_dashboard(
        self,
        request: str,
        document: Document,
        queries: list[dict[str, Any]] | None = None,
        title: str | None = None,
        insights: dict[str, Any] | None = None,
        authoring_document: Document | None = None,
    ) -> SavedDashboard:
        queries = queries or []
        insights = insights or {}
        authoring_document = authoring_document or Document()
        title = title or self._derive_title(request)
        row = models.Dashboard(
            org_id=self.org_id,
            created_by_user_id=self.user_id,
            request=request,
            title=title,
            document=document.to_dict(),
            queries=queries,
            insights=insights,
            authoring_document=authoring_document.to_dict(),
            filters={},
            analysis=None,
        )
        self._db.add(row)
        self._db.flush()
        return SavedDashboard(
            id=row.id,
            request=request,
            title=title,
            created_at=_iso(row.created_at),
            document=document,
            queries=queries,
            insights=insights,
            analysis=None,
            created_by_user_id=row.created_by_user_id,
            authoring_document=authoring_document,
            filters={},
        )

    @staticmethod
    def _to_dashboard(r: models.Dashboard) -> SavedDashboard:
        return SavedDashboard(
            id=r.id,
            request=r.request,
            title=r.title,
            created_at=_iso(r.created_at),
            document=Document.from_dict(r.document or {}),
            queries=list(r.queries or []),
            insights=dict(r.insights or {}),
            analysis=r.analysis,
            created_by_user_id=r.created_by_user_id,
            authoring_document=Document.from_dict(r.authoring_document or {}),
            filters=dict(r.filters or {}),
        )

    def get_dashboard(self, dashboard_id: int) -> SavedDashboard | None:
        row = self._db.execute(
            self._scoped(models.Dashboard).where(models.Dashboard.id == dashboard_id)
        ).scalar_one_or_none()
        return self._to_dashboard(row) if row else None

    def list_dashboards(self, limit: int = 50) -> list[SavedDashboard]:
        rows = self._db.execute(
            self._scoped(models.Dashboard)
            .order_by(models.Dashboard.id.desc())
            .limit(limit)
        ).scalars()
        return [self._to_dashboard(r) for r in rows]

    def set_dashboard_analysis(self, dashboard_id: int, analysis: str) -> bool:
        """Return whether a row was updated (False for another org's id)."""
        result = self._db.execute(
            update(models.Dashboard)
            .where(models.Dashboard.id == dashboard_id)
            .where(models.Dashboard.org_id == self.org_id)
            .values(analysis=analysis)
        )
        return bool(result.rowcount)

    def set_dashboard_filters(
        self, dashboard_id: int, filters: dict[str, Any]
    ) -> bool:
        """Replace a dashboard's filter definitions and query templates.

        Return whether a row was updated -- False for another org's id, which the
        endpoint turns into a 404 rather than confirming the row exists.
        """
        result = self._db.execute(
            update(models.Dashboard)
            .where(models.Dashboard.id == dashboard_id)
            .where(models.Dashboard.org_id == self.org_id)
            .values(filters=filters)
        )
        return bool(result.rowcount)

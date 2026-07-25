"""Few-shot memory + report persistence (Milestone 5).

"Training from user suggestions" is implemented as retrieval-augmented prompting:
user suggestions are stored with an embedding; at report time the most relevant
ones are retrieved and injected into the prompt. No model fine-tuning required.

Also stores generated reports so analysis can reference "own" reports by id.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

from datatalk.agent.blocks import Document, document_to_text
from datatalk.config import Settings, get_settings
from datatalk.context import TenantContext
from datatalk.llm.client import embed


@dataclass
class Suggestion:
    id: int
    text: str
    created_at: str
    score: float = 0.0


@dataclass
class SavedReport:
    id: int
    request: str
    markdown: str
    created_at: str
    document: Document = field(default_factory=Document)
    queries: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class QATurn:
    id: int
    report_id: int
    question: str
    answer_document: Document
    queries: list[dict[str, Any]]
    created_at: str


@dataclass
class SavedDashboard:
    id: int
    request: str
    title: str
    created_at: str
    document: Document = field(default_factory=Document)
    queries: list[dict[str, Any]] = field(default_factory=list)
    analysis: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    """SQLite-backed store for suggestions (few-shot memory) and reports."""

    def __init__(
        self,
        settings: Settings | None = None,
        path: str | None = None,
        ctx: TenantContext | None = None,
    ):
        settings = settings or get_settings()
        # Embeddings now resolve their OpenAI client through a context. This
        # store is replaced wholesale by the Postgres, org-scoped one; until
        # then it falls back to the environment context.
        self.ctx = ctx or TenantContext.from_env()
        self.path = path or settings.datatalk_db_path
        # check_same_thread=False: the web layer may touch a store from the
        # request thread and its streaming generator thread.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request TEXT NOT NULL,
                markdown TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS qa_turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                answer_document TEXT NOT NULL,
                queries TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dashboards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request TEXT NOT NULL,
                title TEXT NOT NULL,
                document TEXT NOT NULL,
                queries TEXT NOT NULL,
                analysis TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Idempotent add-column migration for the block-document feature.

        Older databases have a ``reports`` table with only ``markdown``; add the
        canonical ``document`` and ``queries`` JSON columns without data loss.
        """
        cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(reports)").fetchall()
        }
        if "document" not in cols:
            self._conn.execute("ALTER TABLE reports ADD COLUMN document TEXT")
        if "queries" not in cols:
            self._conn.execute("ALTER TABLE reports ADD COLUMN queries TEXT")

    def close(self) -> None:
        self._conn.close()

    # --- suggestions (few-shot memory) ---

    def add_suggestion(self, text: str) -> Suggestion:
        text = (text or "").strip()
        if not text:
            raise ValueError("Suggestion text is empty.")
        vec = np.asarray(embed([text], self.ctx)[0], dtype=np.float32)
        created = _now()
        cur = self._conn.execute(
            "INSERT INTO suggestions (text, embedding, created_at) VALUES (?, ?, ?)",
            (text, vec.tobytes(), created),
        )
        self._conn.commit()
        return Suggestion(id=cur.lastrowid, text=text, created_at=created)

    def all_suggestions(self) -> list[Suggestion]:
        rows = self._conn.execute(
            "SELECT id, text, created_at FROM suggestions ORDER BY id DESC"
        ).fetchall()
        return [Suggestion(id=r["id"], text=r["text"], created_at=r["created_at"]) for r in rows]

    def delete_suggestion(self, suggestion_id: int) -> None:
        self._conn.execute("DELETE FROM suggestions WHERE id = ?", (suggestion_id,))
        self._conn.commit()

    def retrieve_suggestions(self, query: str, k: int = 5) -> list[Suggestion]:
        """Return up to ``k`` suggestions most relevant to ``query`` (cosine)."""
        rows = self._conn.execute(
            "SELECT id, text, embedding, created_at FROM suggestions"
        ).fetchall()
        if not rows:
            return []

        q = np.asarray(embed([query], self.ctx)[0], dtype=np.float32)
        q_norm = q / (np.linalg.norm(q) + 1e-8)

        scored: list[Suggestion] = []
        for r in rows:
            vec = np.frombuffer(r["embedding"], dtype=np.float32)
            vec_norm = vec / (np.linalg.norm(vec) + 1e-8)
            score = float(np.dot(q_norm, vec_norm))
            scored.append(
                Suggestion(
                    id=r["id"], text=r["text"], created_at=r["created_at"], score=score
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
        created = _now()
        queries = queries or []
        markdown = document_to_text(document)
        cur = self._conn.execute(
            "INSERT INTO reports (request, markdown, document, queries, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                request,
                markdown,
                json.dumps(document.to_dict(), default=str),
                json.dumps(queries, default=str),
                created,
            ),
        )
        self._conn.commit()
        return SavedReport(
            id=cur.lastrowid,
            request=request,
            markdown=markdown,
            created_at=created,
            document=document,
            queries=queries,
        )

    def _row_to_report(self, r: sqlite3.Row) -> SavedReport:
        document = Document.from_dict(json.loads(r["document"])) if r["document"] else Document()
        queries = json.loads(r["queries"]) if r["queries"] else []
        return SavedReport(
            id=r["id"],
            request=r["request"],
            markdown=r["markdown"],
            created_at=r["created_at"],
            document=document,
            queries=queries,
        )

    def get_report(self, report_id: int) -> SavedReport | None:
        r = self._conn.execute(
            "SELECT id, request, markdown, document, queries, created_at "
            "FROM reports WHERE id = ?",
            (report_id,),
        ).fetchone()
        if not r:
            return None
        return self._row_to_report(r)

    def list_reports(self, limit: int = 50) -> list[SavedReport]:
        rows = self._conn.execute(
            "SELECT id, request, markdown, document, queries, created_at "
            "FROM reports ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_report(r) for r in rows]

    # --- Q&A turns ---

    def add_qa_turn(
        self,
        report_id: int,
        question: str,
        answer_document: Document,
        queries: list[dict[str, Any]] | None = None,
    ) -> QATurn:
        created = _now()
        queries = queries or []
        cur = self._conn.execute(
            "INSERT INTO qa_turns (report_id, question, answer_document, queries, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                report_id,
                question,
                json.dumps(answer_document.to_dict(), default=str),
                json.dumps(queries, default=str),
                created,
            ),
        )
        self._conn.commit()
        return QATurn(
            id=cur.lastrowid,
            report_id=report_id,
            question=question,
            answer_document=answer_document,
            queries=queries,
            created_at=created,
        )

    def list_qa_turns(self, report_id: int) -> list[QATurn]:
        rows = self._conn.execute(
            "SELECT id, report_id, question, answer_document, queries, created_at "
            "FROM qa_turns WHERE report_id = ? ORDER BY id ASC",
            (report_id,),
        ).fetchall()
        return [
            QATurn(
                id=r["id"],
                report_id=r["report_id"],
                question=r["question"],
                answer_document=Document.from_dict(json.loads(r["answer_document"])),
                queries=json.loads(r["queries"]) if r["queries"] else [],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # --- dashboards ---

    @staticmethod
    def _derive_title(request: str) -> str:
        title = (request or "").strip().splitlines()[0] if (request or "").strip() else ""
        return (title[:80] or "Dashboard")

    def save_dashboard(
        self,
        request: str,
        document: Document,
        queries: list[dict[str, Any]] | None = None,
        title: str | None = None,
    ) -> SavedDashboard:
        created = _now()
        queries = queries or []
        title = title or self._derive_title(request)
        cur = self._conn.execute(
            "INSERT INTO dashboards (request, title, document, queries, analysis, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                request,
                title,
                json.dumps(document.to_dict(), default=str),
                json.dumps(queries, default=str),
                None,
                created,
            ),
        )
        self._conn.commit()
        return SavedDashboard(
            id=cur.lastrowid,
            request=request,
            title=title,
            created_at=created,
            document=document,
            queries=queries,
            analysis=None,
        )

    def _row_to_dashboard(self, r: sqlite3.Row) -> SavedDashboard:
        document = Document.from_dict(json.loads(r["document"])) if r["document"] else Document()
        queries = json.loads(r["queries"]) if r["queries"] else []
        return SavedDashboard(
            id=r["id"],
            request=r["request"],
            title=r["title"],
            created_at=r["created_at"],
            document=document,
            queries=queries,
            analysis=r["analysis"],
        )

    def get_dashboard(self, dashboard_id: int) -> SavedDashboard | None:
        r = self._conn.execute(
            "SELECT id, request, title, document, queries, analysis, created_at "
            "FROM dashboards WHERE id = ?",
            (dashboard_id,),
        ).fetchone()
        return self._row_to_dashboard(r) if r else None

    def list_dashboards(self, limit: int = 50) -> list[SavedDashboard]:
        rows = self._conn.execute(
            "SELECT id, request, title, document, queries, analysis, created_at "
            "FROM dashboards ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_dashboard(r) for r in rows]

    def set_dashboard_analysis(self, dashboard_id: int, analysis: str) -> None:
        self._conn.execute(
            "UPDATE dashboards SET analysis = ? WHERE id = ?",
            (analysis, dashboard_id),
        )
        self._conn.commit()

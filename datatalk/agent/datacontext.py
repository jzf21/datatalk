"""The documentation agent: reads a workspace's warehouses and writes its context model.

Five passes, of which only Pass C touches a warehouse:

  A. **Survey** (no LLM) — introspect every source and choose which tables are
     worth documenting.
  B. **Plan** (1 call) — name the business *entities* and the analysis
     *playbooks*. This pass is what makes the ontology entity-shaped rather than
     table-shaped: the file list is decided before a single file is written, and
     one entity may span tables and sources.
  C. **Profile** (a capture loop per source) — measure what is actually true:
     enum values, date ranges, key overlap, sentinels.
  D. **Ontology** (1 call per entity) — write each entity file.
  E. **Playbooks + overview** (1 call each) — write the recipes and the front page.

Every call runs on ``ctx.docs_model`` through ``ctx.docs_openai``. The pair
travels together: with a separate ``OPENAI_DOCS_BASE_URL`` an overridden model
name alone would be sent to the wrong endpoint.

Bounded everywhere on purpose. A 4,000-table warehouse must not run away, so
table selection is capped, step counts are capped, entity and playbook counts
are capped, and a wall-clock deadline is checked between units -- on expiry the
run writes what it has and reports ``truncated``. A source that fails is caught
and the rest continues, the same degrade-never-raise rule the catalog follows.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from datatalk.agent.blocks import parse_json_object
from datatalk.agent.sqlloop import EventFn, dataset_previews, run_capture_loop
from datatalk.llm.prompts import (
    DATA_DOC_EDITED_NOTE,
    DATA_DOC_ONTOLOGY_SYSTEM,
    DATA_DOC_OVERVIEW_SYSTEM,
    DATA_DOC_PLAN_SYSTEM,
    DATA_DOC_PLAYBOOK_SYSTEM,
    DATA_DOC_PROFILER_SYSTEM,
    DATA_DOC_REVISION_BLOCK,
)
from datatalk.memory.datacontext import MAX_BODY_CHARS, FileDraft, SavedContextFile
from datatalk.warehouse import catalog

if TYPE_CHECKING:
    from datatalk.context import TenantContext
    from datatalk.warehouse.base import Table

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_ID_COLUMN_RE = re.compile(r"^(.*?)_(id|key|uuid)$", re.IGNORECASE)


@dataclass
class DataContextResult:
    files: list[FileDraft] = field(default_factory=list)
    model: str = ""
    stats: dict[str, Any] = field(default_factory=dict)


# --- one run per org at a time ------------------------------------------------
#
# Per process only, which is stated in the endpoint's docstring too. It stops the
# double-click, which is the case that actually happens; two uvicorn workers
# racing is not worth a database lock here.

_RUNNING: set[UUID] = set()
_RUNNING_LOCK = threading.Lock()


def try_acquire(org_id: UUID) -> bool:
    with _RUNNING_LOCK:
        if org_id in _RUNNING:
            return False
        _RUNNING.add(org_id)
        return True


def release(org_id: UUID) -> None:
    with _RUNNING_LOCK:
        _RUNNING.discard(org_id)


# --- Pass A: which tables are worth documenting -------------------------------


def _select_tables(
    tables: list["Table"], *, limit: int
) -> list["Table"]:
    """Rank by row count, then pull in the dimension tables they point at.

    Row count alone documents fact tables and starves the dimensions that give
    them meaning -- an ontology with `orders` but not `customers` cannot answer a
    customer question. So after ranking, any `*_id` column whose stem names an
    unselected table pulls that table in, within a small extra budget.
    """
    if not tables:
        return []

    ranked = sorted(
        tables,
        key=lambda t: (-(t.total_rows or 0), -len(t.columns), t.qualified_name),
    )
    chosen = ranked[:limit]
    chosen_names = {t.name.lower() for t in chosen}
    by_name: dict[str, "Table"] = {}
    for t in ranked:
        by_name.setdefault(t.name.lower(), t)

    budget = max(1, limit // 4)
    for table in list(chosen):
        if budget <= 0:
            break
        for col in table.columns:
            match = _ID_COLUMN_RE.match(col.name)
            if not match:
                continue
            stem = match.group(1).lower()
            for candidate in (stem, f"{stem}s", stem.rstrip("s")):
                if candidate in chosen_names or candidate not in by_name:
                    continue
                chosen.append(by_name[candidate])
                chosen_names.add(candidate)
                budget -= 1
                break
            if budget <= 0:
                break
    return chosen


# --- Pass B/D/E: one non-tool call --------------------------------------------


def _complete(ctx: "TenantContext", system: str, user: str) -> dict[str, Any]:
    resp = ctx.docs_openai.chat.completions.create(
        model=ctx.docs_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
    )
    return parse_json_object(resp.choices[0].message.content or "")


def _revision_block(existing: SavedContextFile | None) -> str:
    if existing is None or not existing.body_md.strip():
        return ""
    return DATA_DOC_REVISION_BLOCK.format(
        current_body=existing.body_md[:MAX_BODY_CHARS],
        edited_note=DATA_DOC_EDITED_NOTE if existing.human_owned else "",
    )


def _draft_from(obj: dict[str, Any], *, path: str, evidence: list[dict]) -> FileDraft | None:
    body = str(obj.get("body_md") or "").strip()
    if not body:
        return None
    covers = [
        {"source": str(c.get("source", "")), "table": str(c.get("table", ""))}
        for c in (obj.get("covers") or [])
        if isinstance(c, dict) and c.get("table")
    ]
    return FileDraft(
        path=path,
        summary=str(obj.get("summary") or "").strip(),
        body_md=body,
        covers=covers,
        evidence=evidence,
    )


# --- the run ------------------------------------------------------------------


def generate_data_context(
    *,
    ctx: "TenantContext",
    existing: list[SavedContextFile] | None = None,
    mode: str = "revise",
    on_event: EventFn | None = None,
    max_tables_per_source: int = 40,
    max_tables_total: int = 80,
    max_entities: int = 10,
    max_playbooks: int = 6,
    profile_steps: int = 14,
    max_seconds: float = 900.0,
) -> DataContextResult:
    """Run the documentation agent over the org's warehouses.

    In ``revise`` mode each existing file's current body is shown to the writer
    and it is told to revise rather than rewrite -- so a human's edits arrive as
    prompt input, not as something to be merged back afterwards.
    """
    def emit(kind: str, data: dict[str, Any]) -> None:
        if on_event:
            on_event(kind, data)

    prior = {f.path: f for f in (existing or [])} if mode == "revise" else {}
    deadline = time.monotonic() + max_seconds
    out_of_time = lambda: time.monotonic() > deadline  # noqa: E731
    stats: dict[str, Any] = {
        "sources": list(ctx.source_names),
        "tables_seen": 0,
        "tables_profiled": 0,
        "entities": 0,
        "playbooks": 0,
        "queries": 0,
        "truncated": False,
        "failed_sources": [],
    }
    files: list[FileDraft] = []

    # --- Pass A: survey ---
    emit("status", {"message": "Reading the schema…"})
    per_source: dict[str, list["Table"]] = {}
    for ref in ctx.sources:
        try:
            tables = catalog.source_tables(ctx, ref)
        except Exception as exc:  # noqa: BLE001 - one dead source must not stop the rest
            emit("error", {"message": f"{ref.name}: {exc}"})
            stats["failed_sources"].append(ref.name)
            continue
        stats["tables_seen"] += len(tables)
        per_source[ref.name] = _select_tables(tables, limit=max_tables_per_source)

    # Trim to the global cap, keeping each source represented.
    total = sum(len(v) for v in per_source.values())
    if total > max_tables_total and per_source:
        share = max(1, max_tables_total // len(per_source))
        per_source = {k: v[:share] for k, v in per_source.items()}
        stats["truncated"] = True
    stats["tables_profiled"] = sum(len(v) for v in per_source.values())

    if not any(per_source.values()):
        return DataContextResult(files=[], model=ctx.docs_model, stats=stats)

    # --- Pass B: entities and playbooks ---
    emit("status", {"message": "Mapping the business entities…"})
    plan_catalog = "\n\n".join(
        f"SOURCE {name} [{ctx.source(name).type}]\n{catalog.render_table_summary(tables)}"
        for name, tables in per_source.items()
        if tables
    )
    try:
        plan = _complete(
            ctx,
            DATA_DOC_PLAN_SYSTEM.format(
                max_entities=max_entities,
                max_playbooks=max_playbooks,
                schema_context=plan_catalog,
            ),
            "Map this workspace's entities and the analyses its data exists to serve.",
        )
    except Exception as exc:  # noqa: BLE001
        emit("error", {"message": f"Planning failed: {exc}"})
        return DataContextResult(files=[], model=ctx.docs_model, stats=stats)

    entities = [e for e in (plan.get("entities") or []) if _SLUG_RE.match(str(e.get("slug", "")))][
        :max_entities
    ]
    playbooks = [p for p in (plan.get("playbooks") or []) if _SLUG_RE.match(str(p.get("slug", "")))][
        :max_playbooks
    ]
    stats["entities"] = len(entities)
    stats["playbooks"] = len(playbooks)
    # Deliberately not "plan": the report stream already uses that kind for a
    # different shape, and one name meaning two things in the same protocol
    # vocabulary is how a client ends up rendering the wrong thing.
    emit(
        "context_plan",
        {
            "entities": [e.get("slug") for e in entities],
            "playbooks": [p.get("slug") for p in playbooks],
        },
    )

    # --- Pass C: profile, one source at a time ---
    #
    # Sequential on purpose, even though introspection is parallel: the NDJSON
    # stream is one ordered channel, and interleaved "Querying (step N)" lines
    # are unreadable. It also keeps the token burn legible against a wall clock
    # the user is watching.
    datasets: dict[str, Any] = {}
    dataset_sources: dict[str, str] = {}
    evidence: list[dict[str, Any]] = []
    entity_plan = "\n".join(
        f"- {e.get('title', e['slug'])}: "
        + ", ".join(f"{t.get('source')}.{t.get('table')}" for t in (e.get("tables") or []))
        for e in entities
    ) or "(none identified)"

    idx = 1
    for name, tables in per_source.items():
        if not tables or out_of_time():
            stats["truncated"] = stats["truncated"] or bool(tables)
            continue
        emit("status", {"message": f"Profiling {name}…"})
        system = DATA_DOC_PROFILER_SYSTEM.format(
            max_steps=profile_steps,
            source_name=name,
            table_detail=catalog.render_table_detail(tables),
            entity_plan=entity_plan,
        )
        try:
            loop = run_capture_loop(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"Profile source {name!r}."},
                ],
                ctx=ctx,
                max_steps=profile_steps,
                on_event=on_event,
                start_index=idx,
                model=ctx.docs_model,
                openai=ctx.docs_openai,
            )
        except Exception as exc:  # noqa: BLE001
            emit("error", {"message": f"{name}: {exc}"})
            stats["failed_sources"].append(name)
            continue

        datasets.update(loop.datasets)
        dataset_sources.update(loop.dataset_sources)
        evidence.extend(
            {"source": q.get("source", ""), "sql": q.get("sql", ""), "row_count": q.get("row_count")}
            for q in loop.queries
        )
        idx += len(loop.datasets)
        stats["queries"] += len(loop.queries)

    previews = dataset_previews(datasets, sources=dataset_sources)

    # --- Pass D: one file per entity ---
    for entity in entities:
        if out_of_time():
            stats["truncated"] = True
            break
        slug = str(entity["slug"])
        path = f"ontology/{slug}.md"
        emit("status", {"message": f"Writing {path}…"})
        tables_line = ", ".join(
            f"{t.get('source')}.{t.get('table')}" for t in (entity.get("tables") or [])
        )
        detail = _detail_for(per_source, entity.get("tables") or [])
        try:
            obj = _complete(
                ctx,
                DATA_DOC_ONTOLOGY_SYSTEM.format(
                    slug=slug, revision_block=_revision_block(prior.get(path))
                ),
                (
                    f"Entity: {entity.get('title', slug)}\n"
                    f"Tables: {tables_line}\n\n"
                    f"Schema detail:\n{detail}\n\n"
                    f"Profiling results:\n{previews}"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - one bad file must not lose the rest
            emit("error", {"message": f"{path}: {exc}"})
            continue
        draft = _draft_from(obj, path=path, evidence=evidence)
        if draft is not None:
            if not draft.covers:
                draft.covers = [
                    {"source": str(t.get("source", "")), "table": str(t.get("table", ""))}
                    for t in (entity.get("tables") or [])
                    if t.get("table")
                ]
            files.append(draft)
            emit("file", {"path": path, "summary": draft.summary, "revised": path in prior})

    # --- Pass E: playbooks, then the overview ---
    ontology_summaries = "\n".join(f"- {f.path}: {f.summary}" for f in files) or "(none)"

    for playbook in playbooks:
        if out_of_time():
            stats["truncated"] = True
            break
        slug = str(playbook["slug"])
        path = f"playbooks/{slug}.md"
        emit("status", {"message": f"Writing {path}…"})
        try:
            obj = _complete(
                ctx,
                DATA_DOC_PLAYBOOK_SYSTEM.format(
                    slug=slug, revision_block=_revision_block(prior.get(path))
                ),
                (
                    f"Playbook: {playbook.get('title', slug)}\n"
                    f"Why it matters here: {playbook.get('why', '')}\n\n"
                    f"Entity files written:\n{ontology_summaries}\n\n"
                    f"Profiling results:\n{previews}"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            emit("error", {"message": f"{path}: {exc}"})
            continue
        draft = _draft_from(obj, path=path, evidence=evidence)
        if draft is not None:
            files.append(draft)
            emit("file", {"path": path, "summary": draft.summary, "revised": path in prior})

    emit("status", {"message": "Writing overview.md…"})
    try:
        obj = _complete(
            ctx,
            DATA_DOC_OVERVIEW_SYSTEM.format(
                revision_block=_revision_block(prior.get("overview.md"))
            ),
            (
                f"Sources: {', '.join(ctx.source_names)}\n\n"
                f"Context files written:\n{ontology_summaries}\n\n"
                f"Profiling results:\n{previews}"
            ),
        )
    except Exception as exc:  # noqa: BLE001
        emit("error", {"message": f"overview.md: {exc}"})
    else:
        draft = _draft_from(obj, path="overview.md", evidence=[])
        if draft is not None:
            files.append(draft)
            emit(
                "file",
                {"path": "overview.md", "summary": draft.summary, "revised": "overview.md" in prior},
            )

    return DataContextResult(files=files, model=ctx.docs_model, stats=stats)


def _detail_for(
    per_source: dict[str, list["Table"]],
    wanted: list[dict[str, Any]],
) -> str:
    """Full schema detail for just the tables one entity claims."""
    names = {
        (str(w.get("source", "")), str(w.get("table", "")).lower())
        for w in wanted
        if w.get("table")
    }
    parts: list[str] = []
    for source, tables in per_source.items():
        matched = [
            t
            for t in tables
            if any(
                (not src or src == source)
                and (t.qualified_name.lower() == tbl or t.name.lower() == tbl.rsplit(".", 1)[-1])
                for src, tbl in names
            )
        ]
        if matched:
            parts.append(f"SOURCE {source}\n{catalog.render_table_detail(matched)}")
    return "\n\n".join(parts) or "(no matching tables)"


__all__ = [
    "DataContextResult",
    "generate_data_context",
    "release",
    "try_acquire",
]

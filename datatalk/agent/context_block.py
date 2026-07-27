"""Context injection for the agents that have no tools.

The Reporter and the Dashboard author never touch a warehouse and never call a
tool, so the file *tree* that reaches the Planner, Analyst and Q&A is useless to
them -- they could not act on a path. They get bodies pre-selected for them
instead:

- ``overview.md``, always: short by construction, and the place the workspace's
  metric vocabulary, units and naming live.
- With ``queries``, the ontology/playbook files *covering the tables actually
  queried* -- matched against the captured SQL, so a metric definition reaches
  the agent labelling that metric's tile without a tool round-trip.
- For the Planner, ``build_planner_context_block`` picks files lexically
  relevant to the request: the Planner sees the tree in the catalog but has no
  ``read_context``, so relevance-based pre-injection is its only path to a
  playbook's question shape.

The always-on cost stays bounded: everything here is capped and clipped, and an
org with no context model contributes zero bytes to any prompt.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from datatalk.agent.sqlloop import clip_text
from datatalk.llm.prompts import CONTEXT_BLOCK_TEMPLATE

if TYPE_CHECKING:
    from datatalk.context import ContextFile, ContextModel, TenantContext

# The overview is meant to be a couple of paragraphs. This is a backstop against
# an editor pasting a novel into it, not the primary limit (the API caps the
# stored body at memory.datacontext.MAX_BODY_CHARS).
_MAX_OVERVIEW_CHARS = 3000

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    """Lowercase word tokens, with a cheap singular/plural fold.

    "customers" in a request must match an `ontology/customer.md` — a real and
    common miss for exact-token overlap. Both surface forms stay in the set, so
    folding can only add matches, never lose one.
    """
    tokens = set(_TOKEN_RE.findall(text.lower()))
    folded = {t[:-1] for t in tokens if len(t) > 3 and t.endswith("s")}
    return tokens | folded


def files_covering_queries(
    model: "ContextModel",
    queries: list[dict[str, Any]],
    limit: int = 3,
) -> tuple["ContextFile", ...]:
    """Context files whose covered tables appear in the captured SQL.

    Matching is a word-boundary search for the bare table name in each query's
    SQL text, filtered on source when both sides name one. No SQL parsing: the
    worst false positive (a short table name inside a comment) costs one extra
    context file inside the cap, while a parser that chokes costs the feature.
    """
    hits: list[ContextFile] = []
    for f in model.files:
        if f.path == "overview.md":
            continue  # the overview has its own always-on channel
        for q in queries:
            sql = (q.get("sql") or "").lower()
            q_source = q.get("source") or ""
            if not sql:
                continue
            for src, table in f.covers:
                if src and q_source and src != q_source:
                    continue
                bare = (table or "").lower().rsplit(".", 1)[-1]
                if bare and re.search(rf"\b{re.escape(bare)}\b", sql):
                    hits.append(f)
                    break
            else:
                continue
            break
    return tuple(hits[:limit])


def files_relevant_to_request(
    model: "ContextModel",
    request: str,
    limit: int = 2,
) -> tuple["ContextFile", ...]:
    """Files lexically relevant to a request, for agents planning before any SQL.

    Scores token overlap between the request and each file's path stem +
    summary. Playbooks outrank ontology files at equal overlap -- they encode
    question shapes, which is what a planner needs. Zero overlap means zero
    files: silence beats an irrelevant body in every prompt.
    """
    wanted = _tokens(request)
    if not wanted:
        return ()
    scored: list[tuple[float, int, ContextFile]] = []
    for i, f in enumerate(model.files):
        if f.path == "overview.md":
            continue
        stem = f.path.rsplit("/", 1)[-1].removesuffix(".md")
        # Covered table names count too: a request phrased in table vocabulary
        # ("orders by month") should still hit the file documenting that table
        # even when its slug is the business word ("purchases").
        cover_names = " ".join(t.rsplit(".", 1)[-1] for _, t in f.covers)
        overlap = len(wanted & (_tokens(stem) | _tokens(f.summary) | _tokens(cover_names)))
        if overlap == 0:
            continue
        score = overlap + (0.5 if f.path.startswith("playbooks/") else 0.0)
        scored.append((score, i, f))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return tuple(f for _, _, f in scored[:limit])


def _render_files(files: tuple["ContextFile", ...], max_chars_per_file: int) -> str:
    parts = [
        "",
        "=== RELEVANT CONTEXT FILES (this workspace's own definitions — "
        "follow them) ===",
    ]
    for f in files:
        parts.append(f"--- {f.path} ---")
        parts.append(clip_text(f.body_md.strip(), max_chars_per_file))
    parts.append("=== END CONTEXT FILES ===")
    return "\n".join(parts)


def build_context_block(
    ctx: "TenantContext",
    *,
    queries: list[dict[str, Any]] | None = None,
    max_files: int = 3,
    max_chars_per_file: int = 3000,
) -> str:
    """The overview fence, plus the files covering the queried tables.

    Without ``queries`` this is exactly the historical overview-only block.
    With them, the bodies covering the tables the captured SQL actually names
    are appended — the metric formulas and exclusion rules that decide how a
    tile is labelled, which a toolless agent could otherwise never see.
    """
    parts: list[str] = []
    overview = ctx.context_model.get("overview.md")
    if overview is not None and overview.body_md.strip():
        parts.append(
            CONTEXT_BLOCK_TEMPLATE.format(
                overview=overview.body_md[:_MAX_OVERVIEW_CHARS]
            )
        )
    if queries:
        covering = files_covering_queries(ctx.context_model, queries, limit=max_files)
        if covering:
            parts.append(_render_files(covering, max_chars_per_file))
    return "".join(parts)


def build_planner_context_block(ctx: "TenantContext", request: str) -> str:
    """Request-relevant bodies for the Planner, or ``""`` when nothing matches."""
    relevant = files_relevant_to_request(ctx.context_model, request, limit=2)
    if not relevant:
        return ""
    return _render_files(relevant, max_chars_per_file=2500)

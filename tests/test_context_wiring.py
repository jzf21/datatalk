"""How curated context bodies reach the toolless agents.

The Reporter, Dashboard author and Planner cannot call ``read_context``, so the
bodies they need are pre-selected: by the tables the captured SQL actually
names (``files_covering_queries``) or by lexical relevance to the request
(``files_relevant_to_request``). These tests pin the selection rules — the
wrong file in every prompt is a cost, and a missing definition is a mislabeled
KPI.
"""

from __future__ import annotations

from datatalk.agent.context_block import (
    build_context_block,
    build_planner_context_block,
    files_covering_queries,
    files_relevant_to_request,
)
from datatalk.agent.sqlloop import clip_text
from datatalk.context import ContextFile, ContextModel

from tests.conftest import make_ctx


def _file(id, path, summary="", body="body", covers=()):
    return ContextFile(
        id=id, path=path, summary=summary, body_md=body, covers=tuple(covers)
    )


def _model(*files):
    return ContextModel(files=tuple(files))


# --- clip_text ---------------------------------------------------------------


def test_clip_text_marks_the_cut():
    clipped = clip_text("x" * 100, 40)
    assert clipped.startswith("x" * 40)
    assert "truncated at 40 of 100 chars" in clipped


def test_clip_text_leaves_short_text_alone():
    assert clip_text("hello", 40) == "hello"


# --- files_covering_queries --------------------------------------------------


def test_covering_matches_the_bare_table_name_in_sql():
    model = _model(
        _file(1, "ontology/orders.md", covers=[("main", "shop.orders")]),
        _file(2, "ontology/users.md", covers=[("main", "shop.users")]),
    )
    queries = [{"source": "main", "sql": "SELECT count(*) FROM shop.orders"}]

    hit = files_covering_queries(model, queries)
    assert [f.path for f in hit] == ["ontology/orders.md"]


def test_covering_requires_a_word_boundary():
    """`orders` must not match `preorders_log`."""
    model = _model(_file(1, "ontology/orders.md", covers=[("main", "db.orders")]))
    queries = [{"source": "main", "sql": "SELECT * FROM preorders_log"}]
    assert files_covering_queries(model, queries) == ()


def test_covering_respects_the_source_when_both_name_one():
    model = _model(_file(1, "ontology/orders.md", covers=[("billing", "db.orders")]))
    mismatched = [{"source": "events", "sql": "SELECT * FROM db.orders"}]
    blank = [{"source": "", "sql": "SELECT * FROM db.orders"}]

    assert files_covering_queries(model, mismatched) == ()
    assert [f.path for f in files_covering_queries(model, blank)] == [
        "ontology/orders.md"
    ]


def test_covering_excludes_the_overview_and_caps_the_count():
    files = [_file(0, "overview.md", covers=[("", "db.t0")])]
    files += [
        _file(i, f"ontology/t{i}.md", covers=[("", f"db.t{i}")]) for i in range(1, 6)
    ]
    queries = [{"source": "", "sql": "SELECT * FROM t0, t1, t2, t3, t4, t5"}]

    hit = files_covering_queries(_model(*files), queries, limit=3)
    assert len(hit) == 3
    assert all(f.path != "overview.md" for f in hit)


# --- files_relevant_to_request -----------------------------------------------


def test_relevance_picks_the_matching_playbook():
    model = _model(
        _file(1, "ontology/orders.md", summary="what an order row is"),
        _file(2, "playbooks/churn.md", summary="how churn is computed here"),
    )
    hit = files_relevant_to_request(model, "build a monthly churn dashboard")
    assert [f.path for f in hit] == ["playbooks/churn.md"]


def test_relevance_prefers_playbooks_at_equal_overlap():
    model = _model(
        _file(1, "ontology/revenue.md", summary=""),
        _file(2, "playbooks/revenue.md", summary=""),
    )
    hit = files_relevant_to_request(model, "revenue this quarter", limit=1)
    assert [f.path for f in hit] == ["playbooks/revenue.md"]


def test_relevance_returns_nothing_on_zero_overlap():
    model = _model(_file(1, "playbooks/churn.md", summary="churn"))
    assert files_relevant_to_request(model, "warehouse shipping delays") == ()


def test_relevance_folds_plurals():
    """"top customers" must hit `ontology/customer.md` — exact-token overlap
    misses this constantly, and the miss costs the Planner the definition."""
    model = _model(_file(1, "ontology/customer.md", summary="what a customer is"))
    hit = files_relevant_to_request(model, "top customers by revenue")
    assert [f.path for f in hit] == ["ontology/customer.md"]


def test_relevance_matches_covered_table_names():
    """A request in table vocabulary should hit the file documenting that
    table even when its slug uses the business word."""
    model = _model(
        _file(
            1,
            "ontology/purchases.md",
            summary="the purchase lifecycle",
            covers=[("main", "shop.orders")],
        )
    )
    hit = files_relevant_to_request(model, "orders by month")
    assert [f.path for f in hit] == ["ontology/purchases.md"]


# --- the assembled blocks ----------------------------------------------------


def test_build_context_block_without_queries_is_overview_only():
    ctx = make_ctx(
        context_model=_model(
            _file(1, "overview.md", body="We sell widgets."),
            _file(2, "ontology/orders.md", body="secret orders lore"),
        )
    )
    block = build_context_block(ctx)
    assert "We sell widgets." in block
    assert "orders lore" not in block


def test_build_context_block_appends_covering_bodies_for_queries():
    ctx = make_ctx(
        context_model=_model(
            _file(1, "overview.md", body="We sell widgets."),
            _file(
                2,
                "ontology/orders.md",
                body="Exclude test orders (status='test').",
                covers=[("main", "shop.orders")],
            ),
        )
    )
    queries = [{"source": "main", "sql": "SELECT count(*) FROM shop.orders"}]
    block = build_context_block(ctx, queries=queries)

    assert "We sell widgets." in block
    assert "Exclude test orders" in block
    assert "ontology/orders.md" in block


def test_build_context_block_is_empty_for_an_org_without_context():
    ctx = make_ctx()
    assert build_context_block(ctx, queries=[{"source": "", "sql": "SELECT 1"}]) == ""


def test_planner_context_block_renders_relevant_bodies():
    ctx = make_ctx(
        context_model=_model(
            _file(1, "playbooks/churn.md", summary="churn", body="Churn = ..."),
        )
    )
    assert "Churn = ..." in build_planner_context_block(ctx, "weekly churn dashboard")
    assert build_planner_context_block(ctx, "shipping delays") == ""

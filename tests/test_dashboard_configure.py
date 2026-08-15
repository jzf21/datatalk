"""Configuring filters: the rewrite pass and its verification.

The rewrite is the one place a model touches this feature, so these tests are
mostly about what happens when it gets it wrong -- which is the case that has to
be safe, not the case where it gets it right.
"""

import json

from datatalk.dashboards.configure import configure_filters
from datatalk.memory.store import SavedDashboard

from tests.conftest import FakeOpenAI, FakeWarehouse, make_ctx, message, response

DEFS = [
    {"id": "range", "kind": "date_range", "label": "Period"},
    {"id": "region", "kind": "dimension", "label": "Region",
     "column": "region", "source": "main"},
]

GOOD_TEMPLATE = (
    "SELECT month, sum(revenue) AS revenue FROM sales.orders\n"
    "WHERE ({{dt.p_range_all}} OR (ts >= {{dt.p_range_from}} "
    "AND ts < {{dt.p_range_to}}))\n"
    "  AND ({{dt.p_region_all}} OR region {{dt.in:p_region_values}})\n"
    "GROUP BY 1 LIMIT 1000"
)


def _saved(columns=("month", "revenue")):
    return SavedDashboard(
        id=1, request="r", title="t", created_at="2026-08-15T00:00:00+00:00",
        queries=[{
            "dataset_id": "q1", "source": "main",
            "sql": "SELECT month, sum(revenue) AS revenue FROM sales.orders "
                   "GROUP BY 1 LIMIT 1000",
            "columns": list(columns), "row_count": 2,
        }],
    )


def _ctx(scripted, *, columns=("month", "revenue")):
    warehouse = FakeWarehouse(columns=list(columns), rows=[["2026-01", 10]])
    return make_ctx(openai=FakeOpenAI(scripted), warehouses={"main": warehouse}), warehouse


def _reply(sql, filters=("range", "region")):
    return response(message(content=json.dumps({"sql": sql, "filters": list(filters)})))


def test_a_verified_rewrite_is_persisted_with_its_parameters():
    # One call to probe the dimension options, then the rewrite, then the
    # verification run.
    ctx, _ = _ctx([_reply(GOOD_TEMPLATE)])
    filters, report = configure_filters(_saved(), DEFS, ctx=ctx)

    assert report.wired == ["q1"]
    assert report.skipped == []
    template = filters["templates"]["q1"]
    assert template["columns"] == ["month", "revenue"]
    assert {p["name"] for p in template["params"]} == {
        "p_range_all", "p_range_from", "p_range_to",
        "p_region_all", "p_region_values",
    }


def test_a_rewrite_that_changes_the_result_columns_is_rejected():
    """The load-bearing check: blocks address columns by name, so a shifted
    column set would turn every widget on this dataset into a note."""
    ctx, warehouse = _ctx([_reply(GOOD_TEMPLATE)], columns=("month", "revenue"))
    # The verification run comes back with a renamed column.
    original_query = warehouse.query

    def drifting(sql, *, timeout_s, max_rows, parameters=None):
        result = original_query(sql, timeout_s=timeout_s, max_rows=max_rows,
                                parameters=parameters)
        result.columns = ["month", "total"]  # the rewrite renamed the aggregate
        return result

    warehouse.query = drifting
    filters, report = configure_filters(_saved(), DEFS, ctx=ctx)

    assert report.wired == []
    assert filters["templates"] == {}
    assert "changed the result columns" in report.skipped[0]["reason"]


def test_an_invented_placeholder_is_rejected_before_it_runs():
    ctx, _ = _ctx([_reply("SELECT 1 WHERE x = {{dt.p_made_up}}")])
    filters, report = configure_filters(_saved(), DEFS, ctx=ctx)

    assert report.wired == []
    assert filters["templates"] == {}


def test_a_template_that_binds_nothing_is_not_a_template():
    ctx, _ = _ctx([_reply("SELECT month, sum(revenue) AS revenue FROM t GROUP BY 1")])
    _, report = configure_filters(_saved(), DEFS, ctx=ctx)
    assert report.wired == []


def test_the_model_declining_leaves_the_dataset_unfiltered():
    ctx, _ = _ctx([_reply("", filters=[])])
    filters, report = configure_filters(_saved(), DEFS, ctx=ctx)
    assert report.wired == []
    assert report.skipped[0]["dataset_id"] == "q1"
    # The definitions still persist, so the UI can show the controls and say
    # which widgets they do not reach.
    assert [d["id"] for d in filters["filters"]] == ["range", "region"]


def test_unparseable_json_is_survived():
    ctx, _ = _ctx([response(message(content="I'm afraid I can't do that."))])
    _, report = configure_filters(_saved(), DEFS, ctx=ctx)
    assert report.wired == []


def test_dimension_options_are_probed_and_persisted():
    warehouse = FakeWarehouse(columns=["v"], rows=[["EMEA"], ["APAC"]])
    ctx = make_ctx(
        openai=FakeOpenAI([_reply(GOOD_TEMPLATE)]), warehouses={"main": warehouse}
    )
    filters, _ = configure_filters(_saved(), DEFS, ctx=ctx)

    region = next(d for d in filters["filters"] if d["id"] == "region")
    assert region["options"] == ["EMEA", "APAC"]
    assert region["options_truncated"] is False
    # The probe wraps the captured query rather than guessing at a table name.
    assert any("SELECT DISTINCT" in q for q in warehouse.queries)


def test_a_failed_options_probe_leaves_the_filter_usable():
    from datatalk.warehouse.base import WarehouseError

    warehouse = FakeWarehouse(fail=WarehouseError("nope"))
    ctx = make_ctx(
        openai=FakeOpenAI([_reply(GOOD_TEMPLATE)]), warehouses={"main": warehouse}
    )
    filters, _ = configure_filters(_saved(), DEFS, ctx=ctx)
    region = next(d for d in filters["filters"] if d["id"] == "region")
    assert region["options"] == []


def test_the_rewrite_uses_the_author_model_not_the_default():
    ctx, _ = _ctx([_reply(GOOD_TEMPLATE)])
    configure_filters(_saved(), DEFS, ctx=ctx)
    used = ctx.openai.chat.completions.kwargs[0]["model"]
    assert used == ctx.author_model

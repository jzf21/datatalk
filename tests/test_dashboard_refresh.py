"""The refresh runner: re-executing a saved dashboard's queries.

No HTTP and no database here -- the runner takes a SavedDashboard and a
TenantContext and returns a document, so it can be exercised directly against
fake warehouses. The endpoint tests live in test_dashboard_web.py.
"""

import pytest

from datatalk.agent.blocks import Chart, Document, Row, Stat, Table, materialize
from datatalk.agent.executor import QueryResult, UnsafeSQLError
from datatalk.dashboards import refresh as refresh_svc
from datatalk.memory.store import SavedDashboard
from datatalk.warehouse.base import WarehouseError

from tests.conftest import FakeWarehouse, make_ctx


def _authoring():
    return Document(blocks=[
        Row(children=[
            Stat(dataset_id="q1", value_col="issues", label="Issues", width=3),
            Table(dataset_id="q2", columns=["month", "issues"], width=9),
        ]),
        Chart(chart_type="line", title="Trend", dataset_id="q1",
              x_col="month", series_cols=["issues"]),
    ])


def _saved(*, keep_authoring=True, queries=None):
    """A dashboard as it sits in Postgres: materialized doc + provenance.

    ``keep_authoring=False`` reproduces a row saved before the authoring document
    was persisted -- the document still has real blocks, there is just no copy to
    replay, which is exactly the legacy case.
    """
    authoring = _authoring()
    document = materialize(authoring, {"q1": _rows(), "q2": _rows()})
    if queries is None:
        queries = [
            {"dataset_id": "q1", "source": "a", "sql": "SELECT 1",
             "columns": ["month", "issues"], "row_count": 2},
            {"dataset_id": "q2", "source": "a", "sql": "SELECT 2",
             "columns": ["month", "issues"], "row_count": 2},
        ]
    return SavedDashboard(
        id=1,
        request="r",
        title="t",
        created_at="2026-08-15T00:00:00+00:00",
        document=document,
        queries=queries,
        authoring_document=authoring if keep_authoring else Document(),
    )


def _rows(issues=(10, 20)):
    return QueryResult(
        columns=["month", "issues"],
        rows=[["2026-01", issues[0]], ["2026-02", issues[1]]],
        row_count=2,
        truncated=False,
        sql="SELECT ...",
    )


def _ctx(**warehouses):
    return make_ctx(warehouses=warehouses)


def test_a_refresh_reproduces_what_generation_would_have_built():
    """The identity the whole feature rests on: same materialize, same inputs."""
    wh = FakeWarehouse(columns=["month", "issues"],
                       rows=[["2026-01", 10], ["2026-02", 20]])
    saved = _saved()
    out = refresh_svc.refresh_dashboard(saved, ctx=_ctx(a=wh))

    expected = materialize(saved.authoring_document, {"q1": _rows(), "q2": _rows()})
    assert out.document.to_dict() == expected.to_dict()
    assert out.partial is False
    assert out.exact is True
    assert [d.status for d in out.datasets] == ["ok", "ok"]


def test_fresh_numbers_replace_the_snapshot():
    """The actual bug being fixed: the saved document holds 20, the warehouse 99."""
    wh = FakeWarehouse(columns=["month", "issues"],
                       rows=[["2026-01", 98], ["2026-02", 99]])
    saved = _saved()
    assert saved.document.blocks[0].children[0].value == 20

    out = refresh_svc.refresh_dashboard(saved, ctx=_ctx(a=wh))
    assert out.document.blocks[0].children[0].value == 99


@pytest.mark.parametrize(
    "failure,status,reason",
    [
        (WarehouseError("relation does not exist"), "error", "query_failed"),
        (UnsafeSQLError("Forbidden keyword(s) present: DROP."), "rejected", "unsafe_sql"),
    ],
)
def test_one_dead_query_degrades_only_its_own_blocks(failure, status, reason):
    """The catalog's UNAVAILABLE posture: never fail the whole dashboard."""
    good = FakeWarehouse(columns=["month", "issues"],
                         rows=[["2026-01", 10], ["2026-02", 20]])
    bad = FakeWarehouse(fail=failure)
    saved = _saved(queries=[
        {"dataset_id": "q1", "source": "good", "sql": "SELECT 1"},
        {"dataset_id": "q2", "source": "bad", "sql": "SELECT 2"},
    ])

    out = refresh_svc.refresh_dashboard(saved, ctx=_ctx(good=good, bad=bad))

    assert out.partial is True
    by_id = {d.dataset_id: d for d in out.datasets}
    assert by_id["q1"].status == "ok"
    assert by_id["q2"].status == status
    assert by_id["q2"].reason == reason

    # q1's stat still carries a real number; q2's table degraded to a note.
    stat, table = out.document.blocks[0].children
    assert stat.value == 20
    assert table.type == "paragraph"
    assert "unknown dataset 'q2'" in table.text


def test_an_unknown_source_is_reported_not_raised():
    """A source renamed or deleted since the dashboard was saved."""
    wh = FakeWarehouse(columns=["month", "issues"], rows=[["2026-01", 10]])
    saved = _saved(queries=[
        {"dataset_id": "q1", "source": "gone", "sql": "SELECT 1"},
    ])
    out = refresh_svc.refresh_dashboard(saved, ctx=_ctx(a=wh))

    assert [d.status for d in out.datasets] == ["unavailable"]
    assert out.datasets[0].reason == "unknown_source"
    assert out.partial is True


def test_a_legacy_dashboard_refreshes_charts_but_freezes_its_stats():
    """Best-effort: tables and charts rebuild, stat tiles keep their old value."""
    wh = FakeWarehouse(columns=["month", "issues"],
                       rows=[["2026-01", 98], ["2026-02", 99]])
    saved = _saved(keep_authoring=False)
    assert saved.is_refreshable is False

    out = refresh_svc.refresh_dashboard(saved, ctx=_ctx(a=wh))

    assert out.exact is False
    assert out.frozen_stats == 1
    assert out.partial is True
    stat, table = out.document.blocks[0].children
    assert stat.value == 20          # frozen, never guessed at
    assert table.rows[-1] == ["2026-02", 99]   # refreshed
    assert out.document.blocks[1].series[0]["values"] == [98, 99]  # refreshed


def test_a_dashboard_with_no_queries_still_returns_its_document():
    saved = _saved(queries=[])
    out = refresh_svc.refresh_dashboard(saved, ctx=_ctx(a=FakeWarehouse()))
    # Nothing to resolve against, so every data block degrades -- but the call
    # succeeds and the headings survive.
    assert out.datasets == []
    assert out.partial is False


def test_the_deadline_marks_unfinished_queries_and_releases():
    import time

    class SlowWarehouse(FakeWarehouse):
        def query(self, sql, *, timeout_s, max_rows, parameters=None):
            time.sleep(0.3)
            return super().query(sql, timeout_s=timeout_s, max_rows=max_rows,
                                 parameters=parameters)

    saved = _saved(queries=[{"dataset_id": "q1", "source": "a", "sql": "SELECT 1"}])
    out = refresh_svc.refresh_dashboard(
        saved, ctx=_ctx(a=SlowWarehouse()), deadline_s=0.01
    )
    assert [d.status for d in out.datasets] == ["timeout"]
    assert out.datasets[0].reason == "deadline_exceeded"


def test_the_guard_admits_one_refresh_per_dashboard_per_org():
    from uuid import uuid4

    org_a, org_b = uuid4(), uuid4()
    assert refresh_svc.try_acquire(org_a, 1) is True
    assert refresh_svc.try_acquire(org_a, 1) is False   # same dashboard, blocked
    assert refresh_svc.try_acquire(org_a, 2) is True    # different dashboard
    # Keyed by (org, id), so one tenant cannot lock another's dashboard id --
    # and the 409 cannot leak that a foreign org has activity on that id.
    assert refresh_svc.try_acquire(org_b, 1) is True

    refresh_svc.release(org_a, 1)
    assert refresh_svc.try_acquire(org_a, 1) is True
    for org, did in ((org_a, 1), (org_a, 2), (org_b, 1)):
        refresh_svc.release(org, did)


def test_release_is_idempotent():
    from uuid import uuid4

    org = uuid4()
    refresh_svc.release(org, 99)  # never acquired; must not raise

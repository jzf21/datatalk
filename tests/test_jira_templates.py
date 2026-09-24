"""Jira report templates, executed against a real synced store.

Two kinds of test, both needing ``DATATALK_TEST_DATABASE_URL``:

* **The whole catalog runs.** Every widget of every template is bound and
  executed with its defaults and with each filter moved off its default, and
  must return exactly the columns its blocks read. This is what catches a SQL
  typo, a placeholder a filter does not bind, or a guardrail rejection (the
  read-only tokenizer forbids ``END``, so a stray ``CASE`` fails here).
* **The numbers are right.** A hand-designed sprint -- scope committed, added,
  re-estimated, removed, carried over -- whose correct report is worked out
  below, independently of the SQL.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from datatalk.agent.executor import run_sql
from datatalk.auth import orgs as orgs_svc
from datatalk.dashboards import templates as templates_svc
from datatalk.dashboards.filters import build_bindings
from datatalk.dashboards.refresh import refresh_dashboard
from datatalk.dashboards.templates import instantiate as inst_svc
from datatalk.db import models
from tests.jira_fake import make_issue, points_change, sprint, sprint_change
from tests.test_jira_sync import (  # noqa: F401 - fixtures
    _jira_source,
    _sync,
    fake_jira,
    store,
    sync_store_url,
)

TODO, PROG, DONE = "1", "3", "10001"

# Sprint 8 (closed) and sprint 9 (active), on board 1.
S8 = sprint(8, "closed", "2024-03-04 09:00", "2024-03-15 17:00", "2024-03-15 18:00", board=1)
S9 = sprint(9, "active", "2024-03-18 11:00", "2030-04-01 09:00", board=1)


def _bug(issue):
    issue["fields"]["issuetype"] = {"name": "Bug", "subtask": False}
    return issue


def _issues():
    """The designed sprint. Expected report for sprint 8:

    committed  A5 + B3 + D2 + E3 = 13 pts (4 issues)  -- B at its *start* estimate
    added      C5                =  5 pts
    removed    D2                =  2 pts
    completed  A5 + C5 + E3      = 13 pts (3 issues)
    not done   B8                =  8 pts             -- B at its *end* estimate
    """
    return [
        make_issue(
            1, "ABC-1", created="2024-03-01 09:00", updated="2024-03-08 10:00",
            status="Done", category="Done", points=5, sprints=[S8],
            transitions=[("2024-03-05 10:00", TODO, PROG, "alice"),
                         ("2024-03-08 10:00", PROG, DONE, "alice")],
            worklogs=[(501, "2024-03-05 11:00", "alice", 7200)],
            labels=["backend"], parent=("ABC-100", "Epic"),
        ),
        make_issue(
            2, "ABC-2", created="2024-03-01 09:00", updated="2024-03-15 18:00",
            points=8, sprints=[S8, S9], assignee="bob",
            history=[("2024-03-06 09:00", "sam", [points_change(3, 8)]),
                     ("2024-03-15 18:00", "sam", [sprint_change([8], [8, 9])])],
        ),
        make_issue(
            3, "ABC-3", created="2024-03-01 09:00", updated="2024-03-12 10:00",
            status="Done", category="Done", points=5, sprints=[S8],
            transitions=[("2024-03-10 10:00", TODO, PROG, "bob"),
                         ("2024-03-12 10:00", PROG, DONE, "bob")],
            history=[("2024-03-06 09:00", "sam", [sprint_change([], [8])])],
        ),
        _bug(make_issue(
            4, "ABC-4", created="2024-03-01 09:00", updated="2024-03-07 09:00",
            points=2, sprints=[], assignee=None,
            history=[("2024-03-07 09:00", "sam", [sprint_change([8], [])])],
        )),
        make_issue(
            5, "ABC-5", created="2024-03-01 09:00", updated="2024-03-14 10:00",
            status="Done", category="Done", points=3, sprints=[S8],
            transitions=[("2024-03-05 10:00", TODO, PROG, "alice"),
                         ("2024-03-14 10:00", PROG, DONE, "alice")],
        ),
    ]


@pytest.fixture
def jira_ctx(db, store, org_a, user_a, fake_jira):
    fake_jira.issues = _issues()
    conn = _jira_source(db, org_a)
    _sync(org_a, conn)
    ctx = orgs_svc.build_tenant_context(db, org=org_a, user=user_a, role="owner")
    return ctx


def _build(ctx, template_id):
    return inst_svc.build(templates_svc.get(template_id), "jira", ctx)


def _run(ctx, inst, selections=None):
    saved = inst_svc._transient(inst)
    return refresh_dashboard(saved, ctx=ctx, selections=selections or {})


def _records(ctx, inst, widget_key, selections=None):
    """One widget's rows, bound exactly as a refresh binds them."""
    query = next(q for q in inst.queries if q["widget"] == widget_key or
                 _widget_in(inst, q["dataset_id"], widget_key))
    dialect = ctx.warehouse("jira").dialect
    bound = build_bindings(
        inst.filters, selections or {}, {q["dataset_id"]: dialect for q in inst.queries}
    ).bound[query["dataset_id"]]
    result = run_sql(bound.sql, ctx=ctx, source="jira", parameters=bound.parameters)
    return result.to_records()


def _widget_in(inst, dataset_id, widget_key):
    template = templates_svc.get(inst.template["id"])
    widget = template.widget(widget_key)
    return any(q["sql"] == widget.sql and q["dataset_id"] == dataset_id for q in inst.queries)


ALL_TIME = {"period": {"preset": "all_time"}}
SPRINT_8 = {"sprint": {"mode": "ids", "ids": ["8"]}}


# --- the whole catalog runs --------------------------------------------------


TEMPLATE_IDS = [t.id for t in templates_svc.templates_for({"jira"})]


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_every_widget_runs_with_defaults_and_returns_its_columns(jira_ctx, template_id):
    inst = _build(jira_ctx, template_id)
    result = _run(jira_ctx, inst)
    bad = [(d.dataset_id, d.message) for d in result.datasets if d.status != "ok"]
    assert bad == []
    assert result.unfiltered == []

    dialect = jira_ctx.warehouse("jira").dialect
    bindings = build_bindings(inst.filters, {}, {q["dataset_id"]: dialect for q in inst.queries})
    for q in inst.queries:
        bound = bindings.bound[q["dataset_id"]]
        out = run_sql(bound.sql, ctx=jira_ctx, source="jira", parameters=bound.parameters)
        assert out.columns == q["columns"], q["widget"]

    # No block degraded to an "unavailable" note.
    text = str(result.document.to_dict())
    assert "unavailable" not in text


def _non_default_selections(definition):
    kind, opts = definition["kind"], definition.get("options") or []
    if kind == "date_range":
        yield {"preset": "last_7_days"}
        yield {"preset": "custom", "from": "2024-03-01", "to": "2024-03-20"}
    elif kind == "sprint":
        if opts:
            yield {"mode": "ids", "ids": opts[:1]}
        yield {"mode": "last_n", "n": 3}
        if definition.get("multi") is not False:
            yield {"mode": "all"}
    elif opts:
        yield {"values": opts[:2]}


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_every_widget_runs_with_every_filter_moved(jira_ctx, template_id):
    inst = _build(jira_ctx, template_id)
    for definition in inst.filters["filters"]:
        for selection in _non_default_selections(definition):
            result = _run(jira_ctx, inst, {definition["id"]: selection})
            bad = [(d.dataset_id, d.message) for d in result.datasets if d.status != "ok"]
            assert bad == [], (definition["id"], selection)


def test_options_come_from_the_synced_tables(jira_ctx):
    inst = _build(jira_ctx, "jira.sprint_report")
    by_id = {f["id"]: f for f in inst.filters["filters"]}
    assert by_id["sprint"]["options"] == ["9", "8"]  # newest first
    assert by_id["sprint"]["option_groups"] == {"9": "active", "8": "closed"}
    assert by_id["board"]["option_labels"] == {"1": "ABC board"}
    # The unassigned sentinel is an option, labelled for people.
    assignee = by_id["assignee"]
    assert "__unassigned__" in assignee["options"]
    assert assignee["option_labels"]["__unassigned__"] == "Unassigned"
    assert by_id["epic"]["options"] == ["ABC-100"]


# --- the numbers -------------------------------------------------------------


def test_sprint_report_replays_scope_and_estimates(jira_ctx):
    inst = _build(jira_ctx, "jira.sprint_report")
    [summary] = _records(jira_ctx, inst, "summary", SPRINT_8)
    assert summary["sprint"] == "Sprint 8"
    assert {k: float(v) for k, v in summary.items() if k.endswith("_points")} == {
        "committed_points": 13, "completed_points": 13, "added_points": 5,
        "removed_points": 2, "not_completed_points": 8,
    }
    assert (summary["committed_issues"], summary["completed_issues"]) == (4, 3)


def test_sprint_report_burndown(jira_ctx):
    inst = _build(jira_ctx, "jira.sprint_report")
    rows = _records(jira_ctx, inst, "burndown", SPRINT_8)
    assert [r["day"] for r in rows][0] == date(2024, 3, 4)
    assert [r["day"] for r in rows][-1] == date(2024, 3, 15)
    remaining = {r["day"].day: float(r["remaining"]) for r in rows}
    assert remaining[4] == 13          # A5 B3 D2 E3
    assert remaining[6] == 23          # C added (5), B re-estimated 3 -> 8
    assert remaining[7] == 21          # D removed
    assert remaining[8] == 16          # A done
    assert remaining[12] == 11         # C done
    assert remaining[14] == 8          # E done
    assert remaining[15] == 8          # B carried over
    assert float(rows[0]["ideal"]) < 13 and float(rows[-1]["ideal"]) == 0


def test_sprint_report_scope_changes_and_not_completed(jira_ctx):
    inst = _build(jira_ctx, "jira.sprint_report")
    changes = _records(jira_ctx, inst, "scope_changes", SPRINT_8)
    assert [(c["issue"], c["change"], float(c["points"])) for c in changes] == [
        ("ABC-3", "added", 5), ("ABC-4", "removed", 2),
    ]
    [left] = _records(jira_ctx, inst, "not_completed", SPRINT_8)
    assert (left["issue"], left["assignee"], float(left["points"])) == ("ABC-2", "Bob", 8)


def test_active_sprint_is_the_default(jira_ctx):
    inst = _build(jira_ctx, "jira.sprint_report")
    [summary] = _records(jira_ctx, inst, "summary")
    # Sprint 9 started with B carried over at its current estimate.
    assert (summary["sprint"], float(summary["committed_points"])) == ("Sprint 9", 8)


def test_issue_filters_narrow_the_sprint(jira_ctx):
    inst = _build(jira_ctx, "jira.sprint_report")
    [summary] = _records(
        jira_ctx, inst, "summary", {**SPRINT_8, "assignee": {"values": ["__unassigned__"]}}
    )
    # Only D is unassigned: committed 2, then removed.
    assert float(summary["committed_points"]) == 2
    assert float(summary["removed_points"]) == 2


def test_velocity_over_closed_sprints(jira_ctx):
    inst = _build(jira_ctx, "jira.velocity")
    rows = _records(jira_ctx, inst, "committed_vs_completed")
    assert [(r["sprint"], float(r["committed"]), float(r["completed"]),
             float(r["not_completed"]), float(r["completion_pct"])) for r in rows] == [
        ("Sprint 8", 13, 13, 8, 100.0),
    ]


def test_flow_cycle_time_and_cfd(jira_ctx):
    inst = _build(jira_ctx, "jira.flow")
    [summary] = _records(jira_ctx, inst, "flow_summary", ALL_TIME)
    # Cycle times: A 3d, C 2d, E 9d.
    assert summary["finished"] == 3
    assert float(summary["cycle_p50"]) == 3.0
    assert summary["wip"] == 0

    window = {"period": {"preset": "custom", "from": "2024-03-01", "to": "2024-03-20"}}
    cfd = {r["day"]: r for r in _records(jira_ctx, inst, "cfd", window)}
    assert (cfd[date(2024, 3, 1)]["to_do"], cfd[date(2024, 3, 1)]["done"]) == (5, 0)
    day9 = cfd[date(2024, 3, 9)]
    assert (day9["done"], day9["in_progress"], day9["to_do"]) == (1, 1, 3)

    hist = {r["cycle_time"]: r["issues"] for r in _records(jira_ctx, inst, "cycle_histogram", ALL_TIME)}
    assert (hist["2-3 days"], hist["3-5 days"], hist["8-13 days"]) == (1, 1, 1)


def test_backlog_bug_widget_ignores_the_type_filter(jira_ctx):
    inst = _build(jira_ctx, "jira.backlog")
    stories_only = {"issue_type": {"values": ["Story"]}}
    bugs = _records(jira_ctx, inst, "open_bugs", stories_only)
    assert bugs == [{"priority": "Medium", "open_bugs": 1}]
    result = _run(jira_ctx, inst, stories_only)
    bug_ds = next(q["dataset_id"] for q in inst.queries if q["widget"] == "open_bugs")
    # Not wired to the type filter (it is about bugs) nor the period (it is "now").
    assert result.unwired[bug_ds] == ["period", "issue_type"]


def test_people_hours_filter_by_who_logged_them(jira_ctx):
    inst = _build(jira_ctx, "jira.people")
    rows = _records(jira_ctx, inst, "hours_by_person",
                    {**ALL_TIME, "assignee": {"values": ["alice"]}})
    assert [(r["person"], float(r["hours"])) for r in rows] == [("Alice", 2.0)]


# --- endpoints ---------------------------------------------------------------


def _jira_client_org(auth_client, db):
    org = db.get(models.Org, auth_client.org_id)
    conn = _jira_source(db, org)
    _sync(org, conn)
    return org


def test_templates_are_listed_only_with_a_jira_source(auth_client, db, store, fake_jira):
    assert auth_client.get("/api/dashboard-templates").json() == {"templates": []}
    fake_jira.issues = _issues()
    _jira_client_org(auth_client, db)
    listed = auth_client.get("/api/dashboard-templates").json()["templates"]
    assert [t["id"] for t in listed] == TEMPLATE_IDS
    assert all(t["sources"] == ["jira"] for t in listed)


def test_create_from_template_then_refresh(auth_client, db, store, fake_jira):
    fake_jira.issues = _issues()
    _jira_client_org(auth_client, db)
    made = auth_client.post(
        "/api/dashboards/from-template",
        json={"template_id": "jira.sprint_report", "source": "jira"},
    )
    assert made.status_code == 200, made.text
    body = made.json()
    assert body["partial"] is False

    detail = auth_client.get(f"/api/dashboards/{body['dashboard_id']}").json()
    assert detail["template"] == {"id": "jira.sprint_report", "version": 1, "source": "jira"}
    assert detail["editable"] is True
    assert [f["kind"] for f in detail["filters"]["filters"]][:2] == ["dimension", "sprint"]

    fresh = auth_client.post(
        f"/api/dashboards/{body['dashboard_id']}/refresh", json={"filters": SPRINT_8}
    ).json()
    stats = [b for row in fresh["document"]["blocks"] if row["type"] == "row"
             for b in row["children"] if b["type"] == "stat"]
    # numeric sums arrive as Decimal strings, as every Postgres numeric does.
    assert [float(s["value"]) for s in stats][:2] == [13, 13]


@pytest.mark.parametrize(
    "body, code",
    [
        ({"template_id": "jira.nope", "source": "jira"}, "template_not_found"),
        ({"template_id": "jira.flow", "source": "main"}, "source_not_found"),  # wrong type
        ({"template_id": "jira.flow", "source": "theirs"}, "source_not_found"),
    ],
)
def test_create_from_template_404s(auth_client, db, store, fake_jira, org_b, body, code):
    _jira_client_org(auth_client, db)
    _jira_source(db, org_b, name="theirs")  # another org's source is not ours
    resp = auth_client.post("/api/dashboards/from-template", json=body)
    assert (resp.status_code, resp.json()["detail"]) == (404, code)


def test_a_template_dashboards_filters_cannot_be_rewritten(auth_client, db, store, fake_jira):
    fake_jira.issues = _issues()
    _jira_client_org(auth_client, db)
    made = auth_client.post(
        "/api/dashboards/from-template", json={"template_id": "jira.flow", "source": "jira"}
    ).json()
    resp = auth_client.put(
        f"/api/dashboards/{made['dashboard_id']}/filters",
        json={"filters": [{"id": "x", "kind": "dimension", "label": "X", "column": "issue"}]},
    )
    assert (resp.status_code, resp.json()["detail"]) == (409, "template_filters_fixed")


# --- layout editing ----------------------------------------------------------


def _made(auth_client, db, fake_jira, template_id="jira.sprint_report"):
    fake_jira.issues = _issues()
    _jira_client_org(auth_client, db)
    made = auth_client.post(
        "/api/dashboards/from-template", json={"template_id": template_id, "source": "jira"}
    ).json()
    dashboard_id = made["dashboard_id"]
    return dashboard_id, auth_client.get(f"/api/dashboards/{dashboard_id}").json()


def _stats(document):
    return [b for row in document["blocks"] if row["type"] == "row"
            for b in row["children"] if b["type"] == "stat"]


def test_a_layout_edit_is_saved_and_refreshed(auth_client, db, store, fake_jira):
    dashboard_id, detail = _made(auth_client, db, fake_jira)
    doc = detail["authoring_document"]
    first_row = doc["blocks"][0]
    # Drop the last stat, widen the rest, and move the burndown row to the top.
    first_row["children"] = first_row["children"][:3]
    for child in first_row["children"]:
        child["width"] = 4
    doc["blocks"] = [doc["blocks"][1], first_row] + doc["blocks"][2:]

    resp = auth_client.put(f"/api/dashboards/{dashboard_id}/layout",
                           json={"authoring_document": doc})
    assert resp.status_code == 200, resp.text

    fresh = auth_client.post(
        f"/api/dashboards/{dashboard_id}/refresh", json={"filters": SPRINT_8}
    ).json()
    assert fresh["document"]["blocks"][0]["children"][0]["type"] == "chart"
    stats = _stats(fresh["document"])
    assert [s["width"] for s in stats] == [4, 4, 4]
    assert [float(s["value"]) for s in stats] == [13, 13, 5]


def test_a_layout_cannot_carry_a_typed_in_number(auth_client, db, store, fake_jira):
    dashboard_id, detail = _made(auth_client, db, fake_jira)
    doc = detail["authoring_document"]
    doc["blocks"][0]["children"][0]["value"] = 999_999
    auth_client.put(f"/api/dashboards/{dashboard_id}/layout", json={"authoring_document": doc})

    saved = auth_client.get(f"/api/dashboards/{dashboard_id}").json()["authoring_document"]
    assert "value" not in saved["blocks"][0]["children"][0]
    fresh = auth_client.post(
        f"/api/dashboards/{dashboard_id}/refresh", json={"filters": SPRINT_8}
    ).json()
    assert float(_stats(fresh["document"])[0]["value"]) == 13


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["blocks"][0]["children"][0].update(value_col="made_up"),
        lambda d: d["blocks"][0]["children"][0].update(dataset_id="q99"),
        lambda d: d["blocks"][0]["children"].append({"type": "row", "children": []}),
        lambda d: d.pop("blocks"),
    ],
)
def test_a_layout_that_references_no_real_data_is_refused(auth_client, db, store, fake_jira, mutate):
    dashboard_id, detail = _made(auth_client, db, fake_jira)
    doc = detail["authoring_document"]
    mutate(doc)
    resp = auth_client.put(f"/api/dashboards/{dashboard_id}/layout",
                           json={"authoring_document": doc})
    assert (resp.status_code, resp.json()["detail"]) == (400, "layout_invalid")


def test_a_legacy_dashboard_is_not_editable(auth_client, db):
    from datatalk.agent.blocks import Document, Heading
    from tests.conftest import store_for

    org = db.get(models.Org, auth_client.org_id)
    saved = store_for(db, org).save_dashboard("old", Document(blocks=[Heading(text="x")]))
    resp = auth_client.put(f"/api/dashboards/{saved.id}/layout",
                           json={"authoring_document": {"blocks": []}})
    assert (resp.status_code, resp.json()["detail"]) == (409, "not_editable")


def test_a_catalog_widget_can_be_added(auth_client, db, store, fake_jira):
    dashboard_id, detail = _made(auth_client, db, fake_jira)
    assert "removed_stat" in [w["key"] for w in detail["widget_catalog"]]

    resp = auth_client.post(f"/api/dashboards/{dashboard_id}/widgets",
                            json={"widget_key": "removed_stat"})
    assert resp.status_code == 200, resp.text
    new_ds = resp.json()["dataset_id"]
    assert new_ds not in {q["dataset_id"] for q in detail["queries"]}

    fresh = auth_client.post(
        f"/api/dashboards/{dashboard_id}/refresh", json={"filters": SPRINT_8}
    ).json()
    assert fresh["partial"] is False
    added = [s for s in _stats(fresh["document"]) if s["dataset_id"] == new_ds]
    assert [(s["label"], float(s["value"])) for s in added] == [("Removed mid-sprint (pts)", 2)]

    missing = auth_client.post(f"/api/dashboards/{dashboard_id}/widgets",
                               json={"widget_key": "nope"})
    assert (missing.status_code, missing.json()["detail"]) == (404, "widget_not_found")


def test_a_widget_can_be_unwired_and_pinned(auth_client, db, store, fake_jira):
    dashboard_id, detail = _made(auth_client, db, fake_jira)
    summary_ds = detail["authoring_document"]["blocks"][0]["children"][0]["dataset_id"]
    url = f"/api/dashboards/{dashboard_id}/widgets/{summary_ds}/filters"

    # Pin the summary to the unassigned issue, whatever the viewer picks.
    resp = auth_client.put(url, json={"overrides": {"assignee": {"values": ["__unassigned__"]}}})
    assert resp.status_code == 200, resp.text
    fresh = auth_client.post(
        f"/api/dashboards/{dashboard_id}/refresh",
        json={"filters": {**SPRINT_8, "assignee": {"values": ["bob"]}}},
    ).json()
    assert float(_stats(fresh["document"])[0]["value"]) == 2  # D, committed

    # Unwire the assignee filter from it entirely: all assignees again.
    wired = [f for f in detail["filters"]["templates"][summary_ds]["filters"] if f != "assignee"]
    auth_client.put(url, json={"wired": wired})
    fresh = auth_client.post(
        f"/api/dashboards/{dashboard_id}/refresh",
        json={"filters": {**SPRINT_8, "assignee": {"values": ["bob"]}}},
    ).json()
    assert float(_stats(fresh["document"])[0]["value"]) == 13
    assert fresh["unwired"][summary_ds] == ["assignee"]


@pytest.mark.parametrize(
    "body, code",
    [
        ({"wired": ["period"]}, "filter_unknown"),        # not a filter this SQL binds
        ({"overrides": {"nope": {"all": True}}}, "filter_unknown"),
        ({"overrides": {"assignee": {"values": ["mallory"]}}}, "filter_value_invalid"),
    ],
)
def test_widget_filter_edits_are_validated(auth_client, db, store, fake_jira, body, code):
    dashboard_id, detail = _made(auth_client, db, fake_jira)
    summary_ds = detail["authoring_document"]["blocks"][0]["children"][0]["dataset_id"]
    resp = auth_client.put(
        f"/api/dashboards/{dashboard_id}/widgets/{summary_ds}/filters", json=body
    )
    assert (resp.status_code, resp.json()["detail"]) == (400, code)


def test_an_ai_widget_is_appended_and_reported_unfiltered(auth_client, db, store, fake_jira, monkeypatch):
    import datatalk.web.app as app_mod
    from datatalk.agent.dashboard import WidgetResult

    dashboard_id, detail = _made(auth_client, db, fake_jira)
    taken = {q["dataset_id"] for q in detail["queries"]}
    seen = {}

    def fake_widget(request, *, ctx, taken_ids, on_event=None):
        seen["taken"] = taken_ids
        return WidgetResult(
            queries=[{"dataset_id": "q9", "source": "jira", "columns": ["n"], "row_count": 1,
                      "sql": "SELECT count(*) AS n FROM issues"}],
            blocks=[{"type": "stat", "label": "Issues", "dataset_id": "q9", "value_col": "n"}],
        )

    monkeypatch.setattr(app_mod, "generate_widget", fake_widget)
    resp = auth_client.post(f"/api/dashboards/{dashboard_id}/widgets/ai",
                            json={"request": "how many issues"})
    events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    assert [e["kind"] for e in events][-2:] == ["widget", "done"]
    assert seen["taken"] == taken

    fresh = auth_client.post(
        f"/api/dashboards/{dashboard_id}/refresh", json={"filters": SPRINT_8}
    ).json()
    assert [s["value"] for s in _stats(fresh["document"]) if s["dataset_id"] == "q9"] == [5]
    # A model-written query has no template: it must say it ignores the filters.
    assert fresh["unfiltered"] == ["q9"] and fresh["partial"] is True


def test_a_store_synced_before_an_upgrade_must_resync_first(auth_client, db, store, fake_jira):
    from datatalk.integrations import syncstore

    fake_jira.issues = _issues()
    org = db.get(models.Org, auth_client.org_id)
    conn = _jira_source(db, org)
    _sync(org, conn)
    with syncstore.admin_connection() as c:
        schema = conn.sync_state.schema_name
        c.execute(f"UPDATE {schema}._sync_meta SET schema_version = 1")
        c.execute(f"ALTER TABLE {schema}._sync_meta DROP COLUMN time_zone")
    resp = auth_client.post(
        "/api/dashboards/from-template", json={"template_id": "jira.flow", "source": "jira"}
    )
    assert (resp.status_code, resp.json()["detail"]) == (409, "source_needs_sync")

    # And the zone lookup itself tolerates the old shape, for dashboards
    # already built: it falls back to UTC instead of failing every query.
    from datatalk.dashboards.templates.jira import _sql as q

    with syncstore.admin_connection() as c:
        c.execute(f"SET search_path = {schema}")
        assert c.execute("SELECT " + q.TZ).fetchone() == ("UTC",)

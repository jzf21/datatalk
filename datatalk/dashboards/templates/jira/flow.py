"""Flow metrics: cycle time, throughput, cumulative flow and ageing WIP.

* **Cycle time** -- first move into the 'In Progress' category to the last move
  into 'Done', in days. Issues that went straight to Done have none and are
  left out rather than counted as zero.
* **Throughput** -- issues finished per week, where finished is the last move
  into 'Done' (or the resolution date, for issues without that transition).
* **Cumulative flow** -- per day, how many issues sat in each status category,
  replayed from ``status_changes``.
* **Ageing WIP** -- open in-progress issues by how long since they last
  entered 'In Progress'.

Sub-tasks are excluded throughout: they would double-count their parent's work.
"""

from __future__ import annotations

from datatalk.dashboards.templates.base import ReportTemplate, Section, WidgetSpec
from datatalk.dashboards.templates.jira import _sql as q

_SCOPE = ("project", "issue_type", "priority", "assignee", "epic", "label", "component")
_ALL = ("period",) + _SCOPE

_FINISHED = (
    "WITH finished AS (SELECT i.id, i.key, i.summary, i.created, "
    "(SELECT min(c.changed_at) FROM status_changes c WHERE c.issue_id = i.id "
    "AND c.to_category = 'In Progress') AS started, "
    "coalesce((SELECT max(c.changed_at) FROM status_changes c WHERE c.issue_id = i.id "
    "AND c.to_category = 'Done'), i.resolved) AS done_at "
    "FROM issues i WHERE i.status_category = 'Done' AND NOT i.is_subtask AND "
    + q.issue_scope("i", _SCOPE) + "), "
    "in_period AS (SELECT f.*, extract(epoch FROM (f.done_at - f.started)) / 86400.0 "
    "AS cycle_days, extract(epoch FROM (f.done_at - f.created)) / 86400.0 AS lead_days "
    "FROM finished f WHERE f.done_at IS NOT NULL AND " + q.in_period("f.done_at") + ") "
)

_SUMMARY = (
    _FINISHED
    + "SELECT count(*) AS finished, "
    "round(percentile_cont(0.5) WITHIN GROUP (ORDER BY cycle_days) "
    "FILTER (WHERE cycle_days >= 0)::numeric, 1) AS cycle_p50, "
    "round(percentile_cont(0.85) WITHIN GROUP (ORDER BY cycle_days) "
    "FILTER (WHERE cycle_days >= 0)::numeric, 1) AS cycle_p85, "
    "round(percentile_cont(0.95) WITHIN GROUP (ORDER BY cycle_days) "
    "FILTER (WHERE cycle_days >= 0)::numeric, 1) AS cycle_p95, "
    "round(percentile_cont(0.5) WITHIN GROUP (ORDER BY lead_days)::numeric, 1) AS lead_p50, "
    "(SELECT count(*) FROM issues i WHERE i.status_category = 'In Progress' "
    "AND NOT i.is_subtask AND " + q.issue_scope("i", _SCOPE) + ") AS wip "
    "FROM in_period"
)

_HISTOGRAM = (
    _FINISHED
    + "SELECT b.label AS cycle_time, count(p.id) AS issues "
    "FROM (VALUES (1, '< 1 day', 0, 1), (2, '1-2 days', 1, 2), (3, '2-3 days', 2, 3), "
    "(4, '3-5 days', 3, 5), (5, '5-8 days', 5, 8), (6, '8-13 days', 8, 13), "
    "(7, '13-21 days', 13, 21), (8, '21+ days', 21, 1000000)) AS b(ord, label, lo, hi) "
    "LEFT JOIN in_period p ON p.cycle_days >= b.lo AND p.cycle_days < b.hi "
    "GROUP BY b.ord, b.label ORDER BY b.ord"
)

_THROUGHPUT = (
    _FINISHED
    + ", bounds AS (" + q.period_bounds("created::date", cap_days=730) + "), "
    "weeks AS (SELECT generate_series(date_trunc('week', lo), date_trunc('week', hi), "
    "interval '1 week')::date AS week FROM bounds) "
    "SELECT w.week, count(p.id) AS finished FROM weeks w "
    "LEFT JOIN in_period p ON date_trunc('week', " + q.local_day("p.done_at")
    + ")::date = w.week GROUP BY w.week ORDER BY w.week"
)

_CFD = (
    "WITH bounds AS (" + q.period_bounds("created::date", cap_days=180) + "), "
    "days AS (SELECT d::date AS day, ((d + interval '1 day')::timestamp AT TIME ZONE "
    + q.TZ + ") AS day_end FROM bounds, generate_series(lo, hi, interval '1 day') d), "
    "seg AS (SELECT i.id, x.cat, x.t_from, "
    "lead(x.t_from) OVER (PARTITION BY i.id ORDER BY x.t_from) AS t_to "
    "FROM issues i, LATERAL ("
    "SELECT i.created AS t_from, coalesce((SELECT c.from_category FROM status_changes c "
    "WHERE c.issue_id = i.id ORDER BY c.changed_at LIMIT 1), i.status_category) AS cat "
    "UNION ALL SELECT c.changed_at, c.to_category FROM status_changes c "
    "WHERE c.issue_id = i.id) x "
    "WHERE NOT i.is_subtask AND " + q.issue_scope("i", _SCOPE) + ") "
    "SELECT d.day, count(s.id) FILTER (WHERE s.cat = 'Done') AS done, "
    "count(s.id) FILTER (WHERE s.cat = 'In Progress') AS in_progress, "
    "count(s.id) FILTER (WHERE s.cat = 'To Do') AS to_do "
    "FROM days d LEFT JOIN seg s ON s.t_from < d.day_end "
    "AND (s.t_to IS NULL OR s.t_to >= d.day_end) "
    "GROUP BY d.day ORDER BY d.day"
)

_AGEING = (
    "SELECT i.key AS issue, i.summary, i.status, "
    "coalesce(u.display_name, 'Unassigned') AS assignee, "
    "round((extract(epoch FROM (now() - coalesce((SELECT max(c.changed_at) "
    "FROM status_changes c WHERE c.issue_id = i.id AND c.to_category = 'In Progress' "
    "AND c.from_category IS DISTINCT FROM 'In Progress'), i.updated))) / 86400.0)::numeric, 1) "
    "AS days_in_progress "
    "FROM issues i LEFT JOIN users u ON u.account_id = i.assignee_id "
    "WHERE i.status_category = 'In Progress' AND NOT i.is_subtask AND "
    + q.issue_scope("i", _SCOPE) + " ORDER BY days_in_progress DESC, i.key LIMIT 25"
)


def _stat(label: str, col: str, direction: str) -> dict:
    return {"type": "stat", "label": label, "value_col": col, "direction": direction, "width": 3}


FLOW = ReportTemplate(
    id="jira.flow",
    version=1,
    title="Flow metrics",
    description=(
        "Cycle time percentiles and distribution, weekly throughput, a "
        "cumulative flow diagram, and the work in progress that has aged most."
    ),
    family="flow",
    source_types=frozenset({"jira"}),
    min_store_version=q.STORE_VERSION,
    filters=(q.PERIOD, q.PROJECT, q.ISSUE_TYPE, q.PRIORITY, q.ASSIGNEE, q.EPIC, q.LABEL,
             q.COMPONENT),
    widgets=(
        WidgetSpec(
            key="flow_summary",
            title="Flow summary",
            sql=_SUMMARY,
            columns=("finished", "cycle_p50", "cycle_p85", "cycle_p95", "lead_p50", "wip"),
            filters=_ALL,
            blocks=(
                _stat("Finished", "finished", "up_is_good"),
                {**_stat("Cycle time p50 (days)", "cycle_p50", "down_is_good")},
                {**_stat("Cycle time p85 (days)", "cycle_p85", "down_is_good")},
                _stat("Work in progress", "wip", "neutral"),
            ),
        ),
        WidgetSpec(
            key="cycle_histogram",
            title="Cycle time distribution",
            sql=_HISTOGRAM,
            columns=("cycle_time", "issues"),
            filters=_ALL,
            blocks=({
                "type": "chart", "chart_type": "bar", "title": "Cycle time distribution",
                "x_col": "cycle_time", "series_cols": ["issues"], "unit": "count", "width": 6,
            },),
        ),
        WidgetSpec(
            key="throughput",
            title="Weekly throughput",
            sql=_THROUGHPUT,
            columns=("week", "finished"),
            filters=_ALL,
            blocks=({
                "type": "chart", "chart_type": "bar", "title": "Issues finished per week",
                "x_col": "week", "series_cols": ["finished"], "unit": "count", "width": 6,
            },),
        ),
        WidgetSpec(
            key="cfd",
            title="Cumulative flow",
            sql=_CFD,
            columns=("day", "done", "in_progress", "to_do"),
            filters=_ALL,
            blocks=({
                "type": "chart", "chart_type": "area", "stacked": True,
                "title": "Cumulative flow", "x_col": "day",
                "series_cols": ["done", "in_progress", "to_do"], "unit": "count", "width": 12,
            },),
        ),
        WidgetSpec(
            key="ageing_wip",
            title="Ageing work in progress",
            sql=_AGEING,
            columns=("issue", "summary", "status", "assignee", "days_in_progress"),
            # "Now" has no period: the oldest WIP is old whatever window is picked.
            filters=_SCOPE,
            blocks=({"type": "table", "width": 12},),
        ),
    ),
    layout=(
        Section(None, ("flow_summary",)),
        Section(None, ("cycle_histogram", "throughput")),
        Section(None, ("cfd",)),
        Section("Ageing work in progress", ("ageing_wip",)),
    ),
    extra_widgets=(
        WidgetSpec(
            key="cycle_p95",
            title="Cycle time p95",
            sql=_SUMMARY,
            columns=("finished", "cycle_p50", "cycle_p85", "cycle_p95", "lead_p50", "wip"),
            filters=_ALL,
            blocks=(_stat("Cycle time p95 (days)", "cycle_p95", "down_is_good"),),
        ),
        WidgetSpec(
            key="lead_p50",
            title="Lead time p50",
            sql=_SUMMARY,
            columns=("finished", "cycle_p50", "cycle_p85", "cycle_p95", "lead_p50", "wip"),
            filters=_ALL,
            blocks=(_stat("Lead time p50 (days)", "lead_p50", "down_is_good"),),
        ),
    ),
).check()

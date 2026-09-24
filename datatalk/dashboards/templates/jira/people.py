"""People and effort: workload, logged time, and neglected work.

The Person filter means the assignee for issue widgets and the author for
worklog widgets -- "Alice's hours" are the hours Alice logged, on anyone's
issues.
"""

from __future__ import annotations

from datatalk.dashboards.templates.base import ReportTemplate, Section, WidgetSpec
from datatalk.dashboards.templates.jira import _sql as q

_ISSUE_SCOPE = ("project", "issue_type", "assignee", "label")
_ALL = ("period",) + _ISSUE_SCOPE
STALE_DAYS = 14

_SUMMARY = (
    "SELECT count(*) FILTER (WHERE i.status_category <> 'Done') AS open_issues, "
    "count(*) FILTER (WHERE i.status_category <> 'Done' AND i.assignee_id IS NULL) "
    "AS unassigned, "
    "count(*) FILTER (WHERE i.status_category <> 'Done' AND i.updated < now() - interval '"
    + str(STALE_DAYS) + " days') AS stale, "
    "(SELECT round(coalesce(sum(w.time_spent_seconds), 0) / 3600.0, 1) FROM worklogs w "
    "JOIN issues wi ON wi.id = w.issue_id WHERE " + q.in_period("w.started") + " AND "
    + q.issue_scope("wi", _ISSUE_SCOPE, person="w.author_id") + ") AS hours_logged "
    "FROM issues i WHERE NOT i.is_subtask AND " + q.issue_scope("i", _ISSUE_SCOPE)
)

_WORKLOAD = (
    "SELECT coalesce(u.display_name, 'Unassigned') AS assignee, "
    "count(*) FILTER (WHERE i.status_category = 'To Do') AS to_do, "
    "count(*) FILTER (WHERE i.status_category = 'In Progress') AS in_progress "
    "FROM issues i LEFT JOIN users u ON u.account_id = i.assignee_id "
    "WHERE i.status_category <> 'Done' AND NOT i.is_subtask AND "
    + q.issue_scope("i", _ISSUE_SCOPE) + " "
    "GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT 20"
)

_HOURS_BY_PERSON = (
    "SELECT coalesce(u.display_name, w.author_id, 'Unknown') AS person, "
    "round(sum(w.time_spent_seconds) / 3600.0, 1) AS hours "
    "FROM worklogs w JOIN issues i ON i.id = w.issue_id "
    "LEFT JOIN users u ON u.account_id = w.author_id "
    "WHERE " + q.in_period("w.started") + " AND "
    + q.issue_scope("i", _ISSUE_SCOPE, person="w.author_id") + " "
    "GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 20"
)

_HOURS_BY_WEEK = (
    "WITH bounds AS (" + q.period_bounds("created::date") + "), "
    "weeks AS (SELECT generate_series(date_trunc('week', lo), date_trunc('week', hi), "
    "interval '1 week')::date AS week FROM bounds), "
    "logged AS (SELECT w.started, w.time_spent_seconds FROM worklogs w "
    "JOIN issues i ON i.id = w.issue_id WHERE "
    + q.issue_scope("i", _ISSUE_SCOPE, person="w.author_id") + ") "
    "SELECT wk.week, round(coalesce(sum(l.time_spent_seconds), 0) / 3600.0, 1) AS hours "
    "FROM weeks wk LEFT JOIN logged l ON date_trunc('week', " + q.local_day("l.started")
    + ")::date = wk.week GROUP BY wk.week ORDER BY wk.week"
)

_RESOLVED_BY_PERSON = (
    "SELECT coalesce(u.display_name, 'Unassigned') AS assignee, count(*) AS resolved "
    "FROM issues i LEFT JOIN users u ON u.account_id = i.assignee_id "
    "WHERE i.resolved IS NOT NULL AND NOT i.is_subtask AND " + q.in_period("i.resolved")
    + " AND " + q.issue_scope("i", _ISSUE_SCOPE) + " "
    "GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 20"
)

_STALE = (
    "SELECT i.key AS issue, i.summary, i.status, "
    "coalesce(u.display_name, 'Unassigned') AS assignee, "
    "(current_date - " + q.local_day("i.updated") + ") AS days_since_update "
    "FROM issues i LEFT JOIN users u ON u.account_id = i.assignee_id "
    "WHERE i.status_category <> 'Done' AND i.updated < now() - interval '"
    + str(STALE_DAYS) + " days' AND NOT i.is_subtask AND "
    + q.issue_scope("i", _ISSUE_SCOPE) + " ORDER BY i.updated, i.key LIMIT 50"
)


def _stat(label: str, col: str, direction: str) -> dict:
    return {"type": "stat", "label": label, "value_col": col, "direction": direction, "width": 3}


PEOPLE = ReportTemplate(
    id="jira.people",
    version=1,
    title="People & effort",
    description=(
        "Open work per person, time logged, who resolved what, and issues "
        "nobody has touched in two weeks."
    ),
    family="people",
    source_types=frozenset({"jira"}),
    min_store_version=q.STORE_VERSION,
    filters=(q.PERIOD, q.PROJECT, q.ISSUE_TYPE, q.PERSON, q.LABEL),
    widgets=(
        WidgetSpec(
            key="people_summary",
            title="People summary",
            sql=_SUMMARY,
            columns=("open_issues", "unassigned", "stale", "hours_logged"),
            filters=_ALL,
            blocks=(
                _stat("Open issues", "open_issues", "neutral"),
                _stat("Unassigned", "unassigned", "down_is_good"),
                _stat(f"Stale ({STALE_DAYS}+ days)", "stale", "down_is_good"),
                _stat("Hours logged", "hours_logged", "neutral"),
            ),
        ),
        WidgetSpec(
            key="workload",
            title="Open work per person",
            sql=_WORKLOAD,
            columns=("assignee", "to_do", "in_progress"),
            filters=_ISSUE_SCOPE,
            blocks=({
                "type": "chart", "chart_type": "horizontal_bar", "stacked": True,
                "title": "Open work per person", "x_col": "assignee",
                "series_cols": ["in_progress", "to_do"], "unit": "count", "width": 6,
            },),
        ),
        WidgetSpec(
            key="hours_by_person",
            title="Hours logged per person",
            sql=_HOURS_BY_PERSON,
            columns=("person", "hours"),
            filters=_ALL,
            blocks=({
                "type": "chart", "chart_type": "horizontal_bar", "title": "Hours logged",
                "x_col": "person", "series_cols": ["hours"], "unit": "duration", "width": 6,
            },),
        ),
        WidgetSpec(
            key="hours_by_week",
            title="Hours logged per week",
            sql=_HOURS_BY_WEEK,
            columns=("week", "hours"),
            filters=_ALL,
            blocks=({
                "type": "chart", "chart_type": "bar", "title": "Hours logged per week",
                "x_col": "week", "series_cols": ["hours"], "unit": "duration", "width": 6,
            },),
        ),
        WidgetSpec(
            key="resolved_by_person",
            title="Resolved per person",
            sql=_RESOLVED_BY_PERSON,
            columns=("assignee", "resolved"),
            filters=_ALL,
            blocks=({
                "type": "chart", "chart_type": "horizontal_bar", "title": "Resolved",
                "x_col": "assignee", "series_cols": ["resolved"], "unit": "count", "width": 6,
            },),
        ),
        WidgetSpec(
            key="stale",
            title="Stale issues",
            sql=_STALE,
            columns=("issue", "summary", "status", "assignee", "days_since_update"),
            filters=_ISSUE_SCOPE,
            blocks=({"type": "table", "width": 12},),
        ),
    ),
    layout=(
        Section(None, ("people_summary",)),
        Section(None, ("workload", "hours_by_person")),
        Section(None, ("hours_by_week", "resolved_by_person")),
        Section(f"Not updated in {STALE_DAYS}+ days", ("stale",)),
    ),
).check()

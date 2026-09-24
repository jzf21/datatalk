"""Backlog and delivery: inflow vs outflow, bugs, epics, versions, overdue work."""

from __future__ import annotations

from datatalk.dashboards.templates.base import ReportTemplate, Section, WidgetSpec
from datatalk.dashboards.templates.jira import _sql as q

_SCOPE = ("project", "issue_type", "priority", "epic", "label", "component", "fix_version")
_ALL = ("period",) + _SCOPE
# The bug widget is *about* one issue type, so the type filter is not wired to it.
_BUG_SCOPE = tuple(f for f in _SCOPE if f != "issue_type")

_SUMMARY = (
    "SELECT count(*) FILTER (WHERE " + q.in_period("i.created") + ") AS created, "
    "count(*) FILTER (WHERE i.resolved IS NOT NULL AND " + q.in_period("i.resolved")
    + ") AS resolved, "
    "count(*) FILTER (WHERE " + q.in_period("i.created") + ") - count(*) FILTER "
    "(WHERE i.resolved IS NOT NULL AND " + q.in_period("i.resolved") + ") AS net_growth, "
    "count(*) FILTER (WHERE i.status_category <> 'Done') AS open_now "
    "FROM issues i WHERE NOT i.is_subtask AND " + q.issue_scope("i", _SCOPE)
)

_CREATED_RESOLVED = (
    "WITH bounds AS (" + q.period_bounds("created::date") + "), "
    "weeks AS (SELECT generate_series(date_trunc('week', lo), date_trunc('week', hi), "
    "interval '1 week')::date AS week FROM bounds), "
    "scoped AS (SELECT i.created, i.resolved FROM issues i WHERE NOT i.is_subtask AND "
    + q.issue_scope("i", _SCOPE) + ") "
    "SELECT w.week, "
    "(SELECT count(*) FROM scoped s WHERE date_trunc('week', " + q.local_day("s.created")
    + ")::date = w.week) AS created, "
    "(SELECT count(*) FROM scoped s WHERE s.resolved IS NOT NULL AND date_trunc('week', "
    + q.local_day("s.resolved") + ")::date = w.week) AS resolved "
    "FROM weeks w ORDER BY w.week"
)

_OPEN_BUGS = (
    "SELECT coalesce(i.priority, 'None') AS priority, count(*) AS open_bugs "
    "FROM issues i WHERE lower(i.issue_type) = 'bug' AND i.status_category <> 'Done' AND "
    + q.issue_scope("i", _BUG_SCOPE) + " GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 20"
)

_EPICS = (
    "SELECT c.parent_key AS epic, coalesce(max(e.summary), '') AS summary, "
    "count(*) AS issues, count(*) FILTER (WHERE c.status_category = 'Done') AS done, "
    "round(100.0 * count(*) FILTER (WHERE c.status_category = 'Done') / count(*), 0) "
    "AS pct_done, coalesce(sum(c.story_points), 0) AS points, "
    "coalesce(sum(c.story_points) FILTER (WHERE c.status_category = 'Done'), 0) AS points_done "
    "FROM issues c LEFT JOIN issues e ON e.key = c.parent_key "
    "WHERE c.parent_issue_type = 'Epic' AND " + q.issue_scope("c", _SCOPE) + " "
    "GROUP BY c.parent_key "
    "ORDER BY count(*) - count(*) FILTER (WHERE c.status_category = 'Done') DESC, 1 LIMIT 25"
)

_VERSIONS = (
    "SELECT v AS version, count(*) AS issues, "
    "count(*) FILTER (WHERE i.status_category = 'Done') AS done, "
    "round(100.0 * count(*) FILTER (WHERE i.status_category = 'Done') / count(*), 0) "
    "AS pct_done FROM issues i, unnest(i.fix_versions) AS v "
    "WHERE NOT i.is_subtask AND " + q.issue_scope("i", _SCOPE) + " "
    "GROUP BY v ORDER BY v DESC LIMIT 25"
)

_OVERDUE = (
    "SELECT i.key AS issue, i.summary, i.status, "
    "coalesce(u.display_name, 'Unassigned') AS assignee, i.due_date, "
    "(current_date - i.due_date) AS days_overdue "
    "FROM issues i LEFT JOIN users u ON u.account_id = i.assignee_id "
    "WHERE i.due_date < current_date AND i.status_category <> 'Done' AND "
    + q.issue_scope("i", _SCOPE) + " ORDER BY i.due_date, i.key LIMIT 50"
)


def _stat(label: str, col: str, direction: str) -> dict:
    return {"type": "stat", "label": label, "value_col": col, "direction": direction, "width": 3}


BACKLOG = ReportTemplate(
    id="jira.backlog",
    version=1,
    title="Backlog & delivery",
    description=(
        "Created vs resolved over time, open bugs by priority, epic and "
        "fix-version progress, and overdue work."
    ),
    family="backlog",
    source_types=frozenset({"jira"}),
    min_store_version=q.STORE_VERSION,
    filters=(q.PERIOD, q.PROJECT, q.ISSUE_TYPE, q.PRIORITY, q.EPIC, q.LABEL, q.COMPONENT,
             q.FIX_VERSION),
    widgets=(
        WidgetSpec(
            key="backlog_summary",
            title="Backlog summary",
            sql=_SUMMARY,
            columns=("created", "resolved", "net_growth", "open_now"),
            filters=_ALL,
            blocks=(
                _stat("Created", "created", "neutral"),
                _stat("Resolved", "resolved", "up_is_good"),
                _stat("Backlog growth", "net_growth", "down_is_good"),
                _stat("Open now", "open_now", "neutral"),
            ),
        ),
        WidgetSpec(
            key="created_resolved",
            title="Created vs resolved",
            sql=_CREATED_RESOLVED,
            columns=("week", "created", "resolved"),
            filters=_ALL,
            blocks=({
                "type": "chart", "chart_type": "line", "title": "Created vs resolved per week",
                "x_col": "week", "series_cols": ["created", "resolved"], "unit": "count",
                "width": 8,
            },),
        ),
        WidgetSpec(
            key="open_bugs",
            title="Open bugs by priority",
            sql=_OPEN_BUGS,
            columns=("priority", "open_bugs"),
            filters=_BUG_SCOPE,
            blocks=({
                "type": "chart", "chart_type": "horizontal_bar", "title": "Open bugs by priority",
                "x_col": "priority", "series_cols": ["open_bugs"], "unit": "count", "width": 4,
            },),
        ),
        WidgetSpec(
            key="epics",
            title="Epic progress",
            sql=_EPICS,
            columns=("epic", "summary", "issues", "done", "pct_done", "points", "points_done"),
            filters=_SCOPE,
            blocks=({"type": "table", "width": 12},),
        ),
        WidgetSpec(
            key="versions",
            title="Fix version progress",
            sql=_VERSIONS,
            columns=("version", "issues", "done", "pct_done"),
            filters=_SCOPE,
            blocks=({"type": "table", "width": 6},),
        ),
        WidgetSpec(
            key="overdue",
            title="Overdue",
            sql=_OVERDUE,
            columns=("issue", "summary", "status", "assignee", "due_date", "days_overdue"),
            filters=_SCOPE,
            blocks=({"type": "table", "width": 12},),
        ),
    ),
    layout=(
        Section(None, ("backlog_summary",)),
        Section(None, ("created_resolved", "open_bugs")),
        Section("Epic progress", ("epics",)),
        Section("Fix versions", ("versions",)),
        Section("Overdue", ("overdue",)),
    ),
).check()

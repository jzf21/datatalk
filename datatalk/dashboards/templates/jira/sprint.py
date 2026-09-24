"""Sprint report and velocity: scrum reports built on sprint *history*.

Definitions, matching Jira's own Sprint Report where it has one:

* **Committed** -- issues in the sprint when it started, at their estimate then.
* **Added** / **removed** -- issues that entered / left after the start and
  before the end, at their estimate when they moved.
* **Completed** -- in the sprint at its end (close, or now while active) and
  in a Done status category then, at their estimate then.
* **Not completed** -- in the sprint at its end and not Done: the carry-over of
  a closed sprint, the remaining work of an active one.

Unestimated issues count as issues but add no points (NULL, not zero).
"""

from __future__ import annotations

from datatalk.dashboards.templates.base import ReportTemplate, Section, WidgetSpec
from datatalk.dashboards.templates.jira import _sql as q

_SCOPE = ("issue_type", "assignee", "epic", "label")
_ONE = ("board", "sprint") + _SCOPE

# The one sprint a Sprint report is about: active first, then most recent.
_THE_SPRINT = (
    "WITH selected_sprints AS (" + q.selected_sprints(board=True) + "), "
    "sp AS (SELECT s.id, s.name, s.state, s.start_date AS t0, "
    "coalesce(s.complete_date, s.end_date) AS planned_end, "
    "least(coalesce(s.complete_date, now()), now()) AS t1 "
    "FROM selected_sprints s "
    "ORDER BY (s.state = 'active') DESC, s.start_date DESC LIMIT 1), "
    # Every issue that was ever in the sprint before it ended.
    "cand AS (SELECT i.* FROM issues i, sp WHERE EXISTS (SELECT 1 FROM sprint_events e "
    "WHERE e.issue_id = i.id AND e.sprint_id = sp.id AND e.changed_at <= sp.t1) "
    "AND " + q.issue_scope("i", _SCOPE) + "), "
    "facts AS (SELECT c.id, c.key, c.summary, c.status, c.assignee_id, "
    + q.in_sprint_at("c.id", "sp.id", "sp.t0") + " AS at_start, "
    + q.in_sprint_at("c.id", "sp.id", "sp.t1") + " AS at_end, "
    + q.points_at("c", "sp.t0") + " AS pts_start, "
    + q.points_at("c", "sp.t1") + " AS pts_end, "
    + q.done_at("c", "sp.t1") + " AS done_end, "
    "(SELECT min(e.changed_at) FROM sprint_events e WHERE e.issue_id = c.id "
    "AND e.sprint_id = sp.id AND e.action = 'added' "
    "AND e.changed_at > sp.t0 AND e.changed_at <= sp.t1) AS added_at, "
    "(SELECT max(e.changed_at) FROM sprint_events e WHERE e.issue_id = c.id "
    "AND e.sprint_id = sp.id AND e.action = 'removed' "
    "AND e.changed_at > sp.t0 AND e.changed_at <= sp.t1) AS removed_at "
    "FROM cand c, sp), "
    "moved AS (SELECT f.*, "
    + q.points_at("c", "f.added_at") + " AS pts_added, "
    + q.points_at("c", "f.removed_at") + " AS pts_removed "
    "FROM facts f JOIN cand c ON c.id = f.id) "
)

_SUMMARY = (
    _THE_SPRINT
    + "SELECT sp.name AS sprint, sp.state, "
    "coalesce(sum(m.pts_start) FILTER (WHERE m.at_start), 0) AS committed_points, "
    "coalesce(sum(m.pts_end) FILTER (WHERE m.at_end AND m.done_end), 0) AS completed_points, "
    "coalesce(sum(m.pts_added) FILTER (WHERE m.added_at IS NOT NULL AND NOT m.at_start), 0) "
    "AS added_points, "
    "coalesce(sum(m.pts_removed) FILTER (WHERE m.removed_at IS NOT NULL AND NOT m.at_end), 0) "
    "AS removed_points, "
    "coalesce(sum(m.pts_end) FILTER (WHERE m.at_end AND NOT m.done_end), 0) "
    "AS not_completed_points, "
    "count(*) FILTER (WHERE m.at_start) AS committed_issues, "
    "count(*) FILTER (WHERE m.at_end AND m.done_end) AS completed_issues "
    "FROM sp LEFT JOIN moved m ON true GROUP BY sp.name, sp.state"
)

_BURNDOWN = (
    _THE_SPRINT
    + ", days AS (SELECT d::date AS day, "
    "((d + interval '1 day')::timestamp AT TIME ZONE " + q.TZ + ") AS day_end "
    "FROM sp, generate_series((sp.t0 AT TIME ZONE " + q.TZ + ")::date, "
    "(coalesce(sp.planned_end, sp.t1) AT TIME ZONE " + q.TZ + ")::date, "
    "interval '1 day') d), "
    "committed AS (SELECT coalesce(sum(pts_start) FILTER (WHERE at_start), 0) AS pts FROM facts) "
    "SELECT days.day, "
    # Remaining work at the end of each day, up to now; future days stay NULL
    # so the line stops at today instead of dropping to zero.
    "(SELECT r.v FROM (SELECT coalesce(sum(" + q.points_at("c", "least(days.day_end, sp.t1)")
    + "), 0) AS v FROM cand c WHERE "
    + q.in_sprint_at("c.id", "sp.id", "least(days.day_end, sp.t1)") + " AND NOT "
    + q.done_at("c", "least(days.day_end, sp.t1)") + ") r "
    "WHERE days.day_end - interval '1 day' <= sp.t1) AS remaining, "
    "round(committed.pts * greatest(0, 1 - extract(epoch FROM "
    "(least(days.day_end, sp.planned_end) - sp.t0)) / "
    "nullif(extract(epoch FROM (sp.planned_end - sp.t0)), 0))::numeric, 1) AS ideal "
    "FROM days, sp, committed ORDER BY days.day"
)

_SCOPE_CHANGES = (
    _THE_SPRINT
    + "SELECT to_char(e.changed_at AT TIME ZONE " + q.TZ + ", 'YYYY-MM-DD HH24:MI') "
    "AS changed, c.key AS issue, c.summary, e.action AS change, "
    + q.points_at("c", "e.changed_at") + " AS points "
    "FROM sprint_events e JOIN cand c ON c.id = e.issue_id, sp "
    "WHERE e.sprint_id = sp.id AND e.changed_at > sp.t0 AND e.changed_at <= sp.t1 "
    "ORDER BY e.changed_at, c.key LIMIT 200"
)

_NOT_COMPLETED = (
    _THE_SPRINT
    + "SELECT f.key AS issue, f.summary, f.status, "
    "coalesce(u.display_name, 'Unassigned') AS assignee, f.pts_end AS points "
    "FROM facts f LEFT JOIN users u ON u.account_id = f.assignee_id "
    "WHERE f.at_end AND NOT f.done_end "
    "ORDER BY f.pts_end DESC NULLS LAST, f.key LIMIT 200"
)


def _stat(label: str, col: str, direction: str | None = None) -> dict:
    out = {"type": "stat", "label": label, "value_col": col, "width": 3}
    if direction:
        out["direction"] = direction
    return out


SPRINT_REPORT = ReportTemplate(
    id="jira.sprint_report",
    version=1,
    title="Sprint report",
    description=(
        "One sprint: committed vs completed, scope added and removed after it "
        "started, a burndown against the ideal line, and what is not done."
    ),
    family="sprint",
    source_types=frozenset({"jira"}),
    min_store_version=q.STORE_VERSION,
    filters=(q.BOARD, q.SPRINT_ONE, q.ISSUE_TYPE, q.ASSIGNEE, q.EPIC, q.LABEL),
    widgets=(
        WidgetSpec(
            key="summary",
            title="Sprint summary",
            sql=_SUMMARY,
            columns=(
                "sprint", "state", "committed_points", "completed_points", "added_points",
                "removed_points", "not_completed_points", "committed_issues",
                "completed_issues",
            ),
            filters=_ONE,
            blocks=(
                _stat("Committed (pts)", "committed_points", "neutral"),
                _stat("Completed (pts)", "completed_points", "up_is_good"),
                _stat("Added mid-sprint (pts)", "added_points", "down_is_good"),
                _stat("Not completed (pts)", "not_completed_points", "down_is_good"),
            ),
        ),
        WidgetSpec(
            key="burndown",
            title="Burndown",
            sql=_BURNDOWN,
            columns=("day", "remaining", "ideal"),
            filters=_ONE,
            blocks=({
                "type": "chart", "chart_type": "line", "title": "Burndown (story points)",
                "x_col": "day", "series_cols": ["remaining", "ideal"], "width": 8,
            },),
        ),
        WidgetSpec(
            key="scope_changes",
            title="Scope changes",
            sql=_SCOPE_CHANGES,
            columns=("changed", "issue", "summary", "change", "points"),
            filters=_ONE,
            blocks=({"type": "table", "width": 12},),
        ),
        WidgetSpec(
            key="not_completed",
            title="Not completed",
            sql=_NOT_COMPLETED,
            columns=("issue", "summary", "status", "assignee", "points"),
            filters=_ONE,
            blocks=({"type": "table", "width": 12},),
        ),
    ),
    layout=(
        Section(None, ("summary",)),
        Section(None, ("burndown",)),
        Section("Scope changes after the sprint started", ("scope_changes",)),
        Section("Not completed", ("not_completed",)),
    ),
    extra_widgets=(
        WidgetSpec(
            key="removed_stat",
            title="Removed (pts)",
            sql=_SUMMARY,
            columns=(
                "sprint", "state", "committed_points", "completed_points", "added_points",
                "removed_points", "not_completed_points", "committed_issues",
                "completed_issues",
            ),
            filters=_ONE,
            blocks=(_stat("Removed mid-sprint (pts)", "removed_points", "neutral"),),
        ),
    ),
).check()


# --- velocity ----------------------------------------------------------------

_VSCOPE = ("issue_type", "label")
_MANY = ("board", "sprint") + _VSCOPE

_VELOCITY_ROWS = (
    "WITH selected_sprints AS (" + q.selected_sprints(board=True) + "), "
    "sp AS (SELECT s.id, s.name, s.start_date AS t0, "
    "least(coalesce(s.complete_date, now()), now()) AS t1 FROM selected_sprints s), "
    "facts AS (SELECT sp.id AS sprint_id, sp.name, sp.t0, "
    + q.in_sprint_at("i.id", "sp.id", "sp.t0") + " AS at_start, "
    + q.in_sprint_at("i.id", "sp.id", "sp.t1") + " AS at_end, "
    + q.points_at("i", "sp.t0") + " AS pts_start, "
    + q.points_at("i", "sp.t1") + " AS pts_end, "
    + q.done_at("i", "sp.t1") + " AS done_end "
    "FROM sp JOIN issues i ON EXISTS (SELECT 1 FROM sprint_events e "
    "WHERE e.issue_id = i.id AND e.sprint_id = sp.id AND e.changed_at <= sp.t1) "
    "WHERE " + q.issue_scope("i", _VSCOPE) + "), "
    "velocity AS (SELECT name AS sprint, t0, "
    "coalesce(sum(pts_start) FILTER (WHERE at_start), 0) AS committed, "
    "coalesce(sum(pts_end) FILTER (WHERE at_end AND done_end), 0) AS completed, "
    "coalesce(sum(pts_end) FILTER (WHERE at_end AND NOT done_end), 0) AS not_completed, "
    "round(100.0 * coalesce(sum(pts_end) FILTER (WHERE at_end AND done_end), 0) "
    "/ nullif(sum(pts_start) FILTER (WHERE at_start), 0), 1) AS completion_pct "
    "FROM facts GROUP BY sprint_id, name, t0) "
)
_VELOCITY = (
    _VELOCITY_ROWS
    + "SELECT sprint, committed, completed, not_completed, completion_pct "
    "FROM velocity ORDER BY t0"
)
_VELOCITY_SUMMARY = (
    _VELOCITY_ROWS
    + "SELECT round(avg(completed), 1) AS avg_velocity, "
    "round(avg(completion_pct), 1) AS avg_completion_pct, "
    "round(avg(not_completed), 1) AS avg_not_completed, count(*) AS sprints FROM velocity"
)

VELOCITY = ReportTemplate(
    id="jira.velocity",
    version=1,
    title="Velocity",
    description=(
        "Committed vs completed story points across recent sprints, with "
        "completion rate and carry-over."
    ),
    family="sprint",
    source_types=frozenset({"jira"}),
    min_store_version=q.STORE_VERSION,
    filters=(q.BOARD, q.SPRINTS, q.ISSUE_TYPE, q.LABEL),
    widgets=(
        WidgetSpec(
            key="velocity_summary",
            title="Velocity summary",
            sql=_VELOCITY_SUMMARY,
            columns=("avg_velocity", "avg_completion_pct", "avg_not_completed", "sprints"),
            filters=_MANY,
            blocks=(
                _stat("Average velocity (pts)", "avg_velocity", "up_is_good"),
                {**_stat("Average completion", "avg_completion_pct", "up_is_good"), "unit": "%"},
                _stat("Average carry-over (pts)", "avg_not_completed", "down_is_good"),
                _stat("Sprints", "sprints", "neutral"),
            ),
        ),
        WidgetSpec(
            key="committed_vs_completed",
            title="Committed vs completed",
            sql=_VELOCITY,
            columns=("sprint", "committed", "completed", "not_completed", "completion_pct"),
            filters=_MANY,
            blocks=({
                "type": "chart", "chart_type": "bar", "title": "Committed vs completed (pts)",
                "x_col": "sprint", "series_cols": ["committed", "completed"], "width": 8,
            },),
        ),
        WidgetSpec(
            key="completion_rate",
            title="Completion rate",
            sql=_VELOCITY,
            columns=("sprint", "committed", "completed", "not_completed", "completion_pct"),
            filters=_MANY,
            blocks=({
                "type": "chart", "chart_type": "line", "title": "Completion rate",
                "x_col": "sprint", "series_cols": ["completion_pct"], "unit": "percent",
                "width": 4,
            },),
        ),
        WidgetSpec(
            key="carry_over",
            title="Carry-over",
            sql=_VELOCITY,
            columns=("sprint", "committed", "completed", "not_completed", "completion_pct"),
            filters=_MANY,
            blocks=({
                "type": "chart", "chart_type": "bar", "title": "Not completed (pts)",
                "x_col": "sprint", "series_cols": ["not_completed"], "width": 6,
            },),
        ),
        WidgetSpec(
            key="velocity_table",
            title="Per-sprint numbers",
            sql=_VELOCITY,
            columns=("sprint", "committed", "completed", "not_completed", "completion_pct"),
            filters=_MANY,
            blocks=({"type": "table", "width": 6},),
        ),
    ),
    layout=(
        Section(None, ("velocity_summary",)),
        Section(None, ("committed_vs_completed", "completion_rate")),
        Section(None, ("carry_over", "velocity_table")),
    ),
).check()

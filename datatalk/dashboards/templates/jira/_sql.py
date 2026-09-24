"""SQL building blocks shared by the Jira report templates.

Every template runs as the source's read-only role, whose ``search_path`` is
its own schema (``syncstore.provision``), so table names are unqualified and a
template never needs to know which schema a source lives in.

Three constraints shape the SQL here:

* **No ``CASE``.** The read-only guardrail (``agent/executor.py``) forbids the
  word ``END`` on Postgres, as transaction control, and it cannot tell
  ``CASE ... END`` from ``END;``. So conditionals are ``coalesce``/``FILTER``/
  ``greatest`` and "first matching row" subqueries.
* **Point-in-time, not current state.** A sprint's committed scope is what was
  in it when it *started*, at the estimate it had *then*. Jira keeps only the
  current value on the issue, so these helpers replay ``sprint_events``,
  ``field_changes`` and ``status_changes``: the value at T is the ``to`` side of
  the last change at or before T, else the ``from`` side of the first change
  after T, else the current value (it never changed).
* **Days are the site's days.** ``_sync_meta.time_zone`` is the Jira site's
  zone; bucketing by UTC would move late-evening work to the next day.

Strings are concatenated rather than f-formatted: ``{{dt.x}}`` placeholders and
f-string braces do not mix legibly.
"""

from __future__ import annotations

from datatalk.dashboards.templates.base import FilterSpec
from datatalk.integrations.jira.schema import SCHEMA_VERSION

# The store shape these templates are written against: sprint_events,
# field_changes, boards and _sync_meta.time_zone arrived in version 2.
STORE_VERSION = SCHEMA_VERSION

# The site's zone, as a scalar expression. Read through to_jsonb so a store
# synced before the column existed resolves to UTC instead of failing the query.
TZ = "(SELECT coalesce(max(to_jsonb(m) ->> 'time_zone'), 'UTC') FROM _sync_meta m)"

UNASSIGNED = "__unassigned__"

# --- filters -----------------------------------------------------------------

PERIOD = FilterSpec("period", "date_range", "Period", {"preset": "last_90_days"})
PROJECT = FilterSpec(
    "project", "dimension", "Project", {"all": True},
    "SELECT key, key || ' · ' || name FROM projects ORDER BY key",
)
BOARD = FilterSpec(
    "board", "dimension", "Board", {"all": True},
    "SELECT id::text, name FROM boards ORDER BY name",
)
ISSUE_TYPE = FilterSpec(
    "issue_type", "dimension", "Issue type", {"all": True},
    "SELECT DISTINCT issue_type FROM issues WHERE issue_type IS NOT NULL ORDER BY 1",
)
PRIORITY = FilterSpec(
    "priority", "dimension", "Priority", {"all": True},
    "SELECT DISTINCT priority FROM issues WHERE priority IS NOT NULL ORDER BY 1",
)
_PEOPLE_OPTIONS = (
    "SELECT v, l FROM ("
    "SELECT u.account_id AS v, coalesce(u.display_name, u.account_id) AS l, 0 AS o "
    "FROM users u WHERE EXISTS (SELECT 1 FROM issues i WHERE i.assignee_id = u.account_id) "
    "OR EXISTS (SELECT 1 FROM worklogs w WHERE w.author_id = u.account_id) "
    "UNION ALL SELECT '" + UNASSIGNED + "', 'Unassigned', 1"
    ") x ORDER BY o, l"
)
ASSIGNEE = FilterSpec("assignee", "dimension", "Assignee", {"all": True}, _PEOPLE_OPTIONS)
PERSON = FilterSpec("assignee", "dimension", "Person", {"all": True}, _PEOPLE_OPTIONS)
EPIC = FilterSpec(
    "epic", "dimension", "Epic", {"all": True},
    "SELECT DISTINCT c.parent_key, coalesce(e.key || ' · ' || e.summary, c.parent_key) "
    "FROM issues c LEFT JOIN issues e ON e.key = c.parent_key "
    "WHERE c.parent_issue_type = 'Epic' ORDER BY 1",
)
LABEL = FilterSpec(
    "label", "dimension", "Label", {"all": True},
    "SELECT DISTINCT unnest(labels) FROM issues ORDER BY 1",
)
COMPONENT = FilterSpec(
    "component", "dimension", "Component", {"all": True},
    "SELECT DISTINCT unnest(components) FROM issues ORDER BY 1",
)
FIX_VERSION = FilterSpec(
    "fix_version", "dimension", "Fix version", {"all": True},
    "SELECT DISTINCT unnest(fix_versions) FROM issues ORDER BY 1",
)
_SPRINT_OPTIONS = (
    "SELECT id::text, name, state FROM sprints "
    "ORDER BY start_date DESC NULLS FIRST, id DESC"
)
SPRINT_ONE = FilterSpec(
    "sprint", "sprint", "Sprint", {"mode": "active"}, _SPRINT_OPTIONS, multi=False
)
SPRINTS = FilterSpec(
    "sprint", "sprint", "Sprints", {"mode": "last_n", "n": 6}, _SPRINT_OPTIONS
)


# --- predicates --------------------------------------------------------------


def _dim(fid: str, expr: str) -> str:
    return "({{dt.p_" + fid + "_all}} OR " + expr + " {{dt.in:p_" + fid + "_values}})"


def _array(fid: str, expr: str) -> str:
    return (
        "({{dt.p_" + fid + "_all}} OR " + expr
        + " && CAST({{dt.p_" + fid + "_values}} AS text[]))"
    )


def issue_scope(a: str, filters: tuple[str, ...], *, person: str | None = None) -> str:
    """AND-ed predicates for the issue-level dimension filters in ``filters``.

    ``person`` overrides which column the assignee filter reads (worklog widgets
    filter by who logged the time, not who holds the issue).
    """
    columns = {
        "project": _dim("project", a + ".project_key"),
        "issue_type": _dim("issue_type", a + ".issue_type"),
        "priority": _dim("priority", a + ".priority"),
        "assignee": _dim(
            "assignee", "coalesce(" + (person or a + ".assignee_id") + ", '" + UNASSIGNED + "')"
        ),
        "epic": _dim("epic", a + ".parent_key"),
        "label": _array("label", a + ".labels"),
        "component": _array("component", a + ".components"),
        "fix_version": _array("fix_version", a + ".fix_versions"),
    }
    parts = [columns[f] for f in filters if f in columns]
    return " AND ".join(parts) or "true"


def local_day(ts: str) -> str:
    return "(" + ts + " AT TIME ZONE " + TZ + ")::date"


def in_period(ts: str) -> str:
    """``ts`` falls in the period, compared as a local date (``to`` is exclusive)."""
    day = local_day(ts)
    return (
        "({{dt.p_period_all}} OR (" + day + " >= {{dt.p_period_from}} AND "
        + day + " < {{dt.p_period_to}}))"
    )


def period_bounds(earliest: str, cap_days: int = 730) -> str:
    """A ``bounds(lo, hi)`` CTE body: the period's local dates, inclusive.

    "All time" starts at ``earliest`` (a date expression over ``issues``),
    capped to ``cap_days`` back so a daily series stays a sane length.
    """
    return (
        "SELECT {{dt.p_period_from}}::date AS lo, ({{dt.p_period_to}}::date - 1) AS hi "
        "WHERE NOT {{dt.p_period_all}} "
        "UNION ALL "
        "SELECT greatest(coalesce(min(" + earliest + "), current_date - 90), "
        "current_date - " + str(int(cap_days)) + "), current_date "
        "FROM issues HAVING {{dt.p_period_all}}"
    )


def selected_sprints(*, board: bool) -> str:
    """A ``selected_sprints`` CTE body for the sprint (and board) filters.

    "active" falls back to the most recently closed sprint when none is
    active, so a report opened between sprints shows the one that just ended
    rather than nothing.
    """
    b = _dim("board", "s.board_id::text") if board else "true"
    b2 = _dim("board", "s2.board_id::text") if board else "true"
    mode = "CAST({{dt.p_sprint_mode}} AS text)"
    latest_closed = (
        "SELECT s2.id FROM sprints s2 WHERE s2.state = 'closed' AND " + b2 + " "
        "ORDER BY coalesce(s2.complete_date, s2.end_date) DESC NULLS LAST, s2.id DESC"
    )
    return (
        "SELECT s.* FROM sprints s WHERE s.start_date IS NOT NULL AND " + b + " AND ("
        + mode + " = 'all' "
        "OR (" + mode + " = 'active' AND s.state = 'active') "
        "OR (" + mode + " = 'active' AND NOT EXISTS (SELECT 1 FROM sprints s2 "
        "WHERE s2.state = 'active' AND " + b2 + ") AND s.id IN (" + latest_closed + " LIMIT 1)) "
        "OR (" + mode + " = 'ids' AND s.id::text {{dt.in:p_sprint_ids}}) "
        "OR (" + mode + " = 'last_n' AND s.id IN (" + latest_closed
        + " LIMIT {{dt.p_sprint_n}})))"
    )


# --- point in time -----------------------------------------------------------


def in_sprint_at(issue_id: str, sprint_id: str, t: str) -> str:
    """Was the issue in the sprint at ``t``? (Its last event then was 'added'.)"""
    return (
        "coalesce((SELECT pit_e.action = 'added' FROM sprint_events pit_e "
        "WHERE pit_e.issue_id = " + issue_id + " AND pit_e.sprint_id = " + sprint_id
        + " AND pit_e.changed_at <= " + t + " ORDER BY pit_e.changed_at DESC LIMIT 1), false)"
    )


def points_at(a: str, t: str) -> str:
    """The issue's story points at ``t`` (NULL = unestimated then).

    The ``pit_`` aliases keep these subqueries from shadowing a caller's own
    (``c``/``e``/``f`` are what templates name their tables).
    """
    f = "FROM field_changes pit_f WHERE pit_f.issue_id = " + a + ".id AND pit_f.field = 'story_points'"
    return (
        "(SELECT pit_v.p FROM ("
        "(SELECT 1 AS o, pit_f.to_value::numeric AS p " + f + " AND pit_f.changed_at <= " + t
        + " ORDER BY pit_f.changed_at DESC LIMIT 1) UNION ALL "
        "(SELECT 2, pit_f.from_value::numeric " + f + " AND pit_f.changed_at > " + t
        + " ORDER BY pit_f.changed_at LIMIT 1) UNION ALL "
        "(SELECT 3, " + a + ".story_points)"
        ") pit_v ORDER BY pit_v.o LIMIT 1)"
    )


def category_at(a: str, t: str) -> str:
    """The issue's status category at ``t``."""
    c = "FROM status_changes pit_c WHERE pit_c.issue_id = " + a + ".id"
    return (
        "(SELECT pit_v.cat FROM ("
        "(SELECT 1 AS o, pit_c.to_category AS cat " + c + " AND pit_c.changed_at <= " + t
        + " ORDER BY pit_c.changed_at DESC LIMIT 1) UNION ALL "
        "(SELECT 2, pit_c.from_category " + c + " AND pit_c.changed_at > " + t
        + " ORDER BY pit_c.changed_at LIMIT 1) UNION ALL "
        "(SELECT 3, " + a + ".status_category)"
        ") pit_v ORDER BY pit_v.o LIMIT 1)"
    )


def done_at(a: str, t: str) -> str:
    return "coalesce(" + category_at(a, t) + " = 'Done', false)"

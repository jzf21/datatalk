"""Copy Jira into a source's schema of the sync store.

Pure with respect to the app database: it takes a :class:`JiraSourceConfig`
(credentials, scope, schema, cursor) and returns a :class:`SyncResult`. Reading
and writing ``source_sync_state`` is :mod:`datatalk.integrations.jira.service`'s
job, so this module can be driven by the UI, the CLI, or a test identically.

Incremental by default: issues updated since the cursor (minus an overlap --
upserts are idempotent, a missed edit is not) are re-fetched and **replace**
their rows, children included. Jira never reports deletions to a search, so
only a *full* sync can notice an issue that is gone; it deletes whatever it did
not see, and only after the whole walk succeeded.

Each page is written in its own transaction, and the cursor is returned only
from a completed run: a crash mid-way leaves earlier pages written and the
cursor where it was, so the next run re-reads rather than skips.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import psycopg
from psycopg import sql as pgsql

from datatalk.config import Settings
from datatalk.integrations import syncstore
from datatalk.integrations.jira import schema as jschema
from datatalk.integrations.jira.client import JiraClient

# Re-read this much before the cursor. JQL dates have minute precision and are
# evaluated in the *user's* timezone, so a small overlap absorbs both.
_OVERLAP = timedelta(minutes=2)
# The enhanced search refuses unbounded JQL; a full sync with no scope needs
# *some* restriction, and this one excludes nothing.
_EVERYTHING = 'updated >= "1970/01/01 00:00"'
_ORDER_BY_RE = re.compile(r"\border\s+by\b.*$", re.IGNORECASE | re.DOTALL)

_STORY_POINT_NAMES = ("story points", "story point estimate")
_SPRINT_CUSTOM_TYPE = "com.pyxis.greenhopper.jira:gh-sprint"

OnEvent = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class JiraSourceConfig:
    site: str
    email: str
    api_token: str
    scope_jql: str | None
    schema: str
    cursor: datetime | None

    def __repr__(self) -> str:  # never render the token
        return f"<JiraSourceConfig {self.email}@{self.site} schema={self.schema}>"


@dataclass
class SyncResult:
    cursor: datetime | None
    full: bool
    stats: dict[str, Any] = field(default_factory=dict)


# --- JQL ---------------------------------------------------------------------


def build_jql(scope: str | None, since: datetime | None, tz: str) -> str:
    """``(<scope>) AND updated >= "<since, in the user's tz>" ORDER BY updated ASC``.

    A trailing ``ORDER BY`` in the admin's scope is dropped -- ours must win,
    or the cursor stops meaning "everything before this is done".
    """
    parts: list[str] = []
    scope = _ORDER_BY_RE.sub("", (scope or "").strip()).strip()
    if scope:
        parts.append(f"({scope})")
    if since is None:
        parts.append(_EVERYTHING)
    else:
        local = (since - _OVERLAP).astimezone(_zone(tz))
        parts.append(f'updated >= "{local.strftime("%Y/%m/%d %H:%M")}"')
    return " AND ".join(parts) + " ORDER BY updated ASC"


def _zone(name: str | None):
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


# --- parsing -----------------------------------------------------------------


def parse_ts(value: Any) -> datetime | None:
    """Jira's ``2024-01-02T10:11:12.000+0000``. None for anything unparseable."""
    if not value:
        return None
    s = str(value)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


@dataclass(frozen=True)
class FieldMap:
    story_points: str | None
    sprint: str | None

    @classmethod
    def discover(cls, fields: list[dict[str, Any]]) -> "FieldMap":
        by_name: dict[str, str] = {}
        sprint = None
        for f in fields or []:
            name = str(f.get("name") or "").strip().lower()
            fid = f.get("id")
            if not fid:
                continue
            by_name.setdefault(name, fid)
            if (f.get("schema") or {}).get("custom") == _SPRINT_CUSTOM_TYPE:
                sprint = sprint or fid
        story = next((by_name[n] for n in _STORY_POINT_NAMES if n in by_name), None)
        return cls(story_points=story, sprint=sprint)

    def request_fields(self) -> list[str]:
        base = [
            "summary", "status", "issuetype", "priority", "assignee", "reporter",
            "created", "updated", "resolutiondate", "duedate", "labels",
            "components", "fixVersions", "parent", "project", "worklog",
        ]
        return base + [f for f in (self.story_points, self.sprint) if f]


@dataclass
class PageRows:
    projects: dict[str, tuple] = field(default_factory=dict)
    users: dict[str, tuple] = field(default_factory=dict)
    sprints: dict[int, tuple] = field(default_factory=dict)
    issues: list[tuple] = field(default_factory=list)
    status_changes: list[tuple] = field(default_factory=list)
    issue_sprints: list[tuple] = field(default_factory=list)
    worklogs: dict[int, tuple] = field(default_factory=dict)
    max_updated: datetime | None = None

    @property
    def issue_ids(self) -> list[int]:
        return [r[0] for r in self.issues]


def _user(rows: PageRows, u: dict[str, Any] | None) -> str | None:
    if not u or not u.get("accountId"):
        return None
    aid = str(u["accountId"])
    rows.users[aid] = (aid, u.get("displayName"), u.get("active"), u.get("accountType"))
    return aid


def parse_page(
    issues: list[dict[str, Any]],
    fmap: FieldMap,
    categories: dict[str, str],
    client: JiraClient | None = None,
) -> PageRows:
    """Turn one search page into rows. ``client`` fetches truncated
    changelogs/worklogs; without it the inline (possibly partial) lists are used.
    """
    rows = PageRows()
    for issue in issues:
        f = issue.get("fields") or {}
        iid = int(issue["id"])
        key = str(issue["key"])

        project = f.get("project") or {}
        if project.get("id"):
            rows.projects[str(project["id"])] = (
                str(project["id"]),
                project.get("key"),
                project.get("name") or project.get("key"),
                project.get("projectTypeKey"),
            )

        status = f.get("status") or {}
        itype = f.get("issuetype") or {}
        parent = f.get("parent") or {}
        updated = parse_ts(f.get("updated"))
        created = parse_ts(f.get("created")) or updated
        if updated is None:
            continue  # cannot place it on the cursor; Jira always sends it
        if rows.max_updated is None or updated > rows.max_updated:
            rows.max_updated = updated

        rows.issues.append(
            (
                iid,
                key,
                project.get("key") or key.split("-", 1)[0],
                f.get("summary"),
                itype.get("name"),
                bool(itype.get("subtask")),
                status.get("name"),
                (status.get("statusCategory") or {}).get("name"),
                (f.get("priority") or {}).get("name"),
                _user(rows, f.get("assignee")),
                _user(rows, f.get("reporter")),
                created,
                updated,
                parse_ts(f.get("resolutiondate")),
                _parse_date(f.get("duedate")),
                _number(f.get(fmap.story_points)) if fmap.story_points else None,
                [str(x) for x in (f.get("labels") or [])],
                [c.get("name") for c in (f.get("components") or []) if c.get("name")],
                [v.get("name") for v in (f.get("fixVersions") or []) if v.get("name")],
                parent.get("key"),
                ((parent.get("fields") or {}).get("issuetype") or {}).get("name"),
            )
        )

        # Sprints: the sprint custom field carries full sprint objects, so the
        # Agile API (and the board permissions it needs) is not required.
        for sp in (f.get(fmap.sprint) if fmap.sprint else None) or []:
            if not isinstance(sp, dict) or sp.get("id") is None:
                continue
            sid = int(sp["id"])
            rows.sprints[sid] = (
                sid,
                sp.get("boardId"),
                sp.get("name"),
                sp.get("state"),
                parse_ts(sp.get("startDate")),
                parse_ts(sp.get("endDate")),
                parse_ts(sp.get("completeDate")),
                sp.get("goal"),
            )
            rows.issue_sprints.append((iid, sid))

        # Changelog -> status transitions.
        log = issue.get("changelog") or {}
        histories = list(log.get("histories") or [])
        if client is not None and int(log.get("total") or 0) > len(histories):
            histories = client.changelog(str(iid))
        for h in histories:
            at = parse_ts(h.get("created"))
            if at is None:
                continue
            author = _user(rows, h.get("author"))
            for item in h.get("items") or []:
                if (item.get("fieldId") or item.get("field")) != "status":
                    continue
                rows.status_changes.append(
                    (
                        iid,
                        key,
                        at,
                        author,
                        item.get("fromString"),
                        item.get("toString"),
                        categories.get(str(item.get("from"))),
                        categories.get(str(item.get("to"))),
                    )
                )

        # Worklogs.
        wl = f.get("worklog") or {}
        entries = list(wl.get("worklogs") or [])
        if client is not None and int(wl.get("total") or 0) > len(entries):
            entries = client.worklogs(str(iid))
        for w in entries:
            started = parse_ts(w.get("started"))
            if started is None or w.get("id") is None:
                continue
            wid = int(w["id"])
            rows.worklogs[wid] = (
                wid,
                iid,
                key,
                _user(rows, w.get("author")),
                started,
                int(w.get("timeSpentSeconds") or 0),
            )
    return rows


# --- writing -----------------------------------------------------------------


def ensure_tables(conn: psycopg.Connection, schema: str, site: str) -> bool:
    """Create the tables if absent or of an older version. True = rebuilt.

    A rebuild drops and recreates rather than migrating: the store is a cache
    of Jira, so the cheap correct move is a full resync.
    """
    row = None
    try:
        with conn.transaction():
            row = conn.execute(
                pgsql.SQL("SELECT schema_version FROM {}").format(
                    pgsql.Identifier(schema, "_sync_meta")
                )
            ).fetchone()
    except psycopg.errors.UndefinedTable:
        row = None
    if row and row[0] == jschema.SCHEMA_VERSION:
        return False

    ident = pgsql.Identifier(schema)
    with conn.transaction():
        conn.execute(pgsql.SQL("SET LOCAL search_path = {}").format(ident))
        for name, _ in reversed(jschema.TABLES):
            conn.execute(
                pgsql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                    pgsql.Identifier(schema, name)
                )
            )
        for _, ddl in jschema.TABLES:
            conn.execute(ddl)
        for ddl in jschema.INDEXES:
            conn.execute(ddl)
        for table, comment in jschema.TABLE_COMMENTS.items():
            conn.execute(
                pgsql.SQL("COMMENT ON TABLE {} IS {}").format(
                    pgsql.Identifier(schema, table), pgsql.Literal(comment)
                )
            )
        for table, cols in jschema.COLUMN_COMMENTS.items():
            for col, comment in cols.items():
                conn.execute(
                    pgsql.SQL("COMMENT ON COLUMN {} IS {}").format(
                        pgsql.Identifier(schema, table, col), pgsql.Literal(comment)
                    )
                )
        conn.execute(
            "INSERT INTO _sync_meta (schema_version, site) VALUES (%s, %s)",
            (jschema.SCHEMA_VERSION, site),
        )
    return True


def write_page(conn: psycopg.Connection, schema: str, rows: PageRows) -> None:
    """Upsert one page atomically; an issue's children are replaced wholesale."""
    if not rows.issues:
        return
    ids = rows.issue_ids
    keys = [r[1] for r in rows.issues]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(pgsql.SQL("SET LOCAL search_path = {}").format(pgsql.Identifier(schema)))
        if rows.projects:
            cur.executemany(
                "INSERT INTO projects (id, key, name, project_type) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET key = EXCLUDED.key, name = EXCLUDED.name, "
                "project_type = EXCLUDED.project_type",
                list(rows.projects.values()),
            )
        if rows.users:
            cur.executemany(
                "INSERT INTO users (account_id, display_name, active, account_type) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (account_id) DO UPDATE SET "
                "display_name = EXCLUDED.display_name, active = EXCLUDED.active, "
                "account_type = EXCLUDED.account_type",
                list(rows.users.values()),
            )
        if rows.sprints:
            cur.executemany(
                "INSERT INTO sprints (id, board_id, name, state, start_date, end_date, "
                "complete_date, goal) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET board_id = EXCLUDED.board_id, "
                "name = EXCLUDED.name, state = EXCLUDED.state, "
                "start_date = EXCLUDED.start_date, end_date = EXCLUDED.end_date, "
                "complete_date = EXCLUDED.complete_date, goal = EXCLUDED.goal",
                list(rows.sprints.values()),
            )
        # An issue moved between projects frees its old key; a stale row still
        # holding a key another issue now has would break the upsert below.
        cur.execute(
            "DELETE FROM issues WHERE key = ANY(%s) AND NOT (id = ANY(%s))",
            (keys, ids),
        )
        cur.executemany(
            "INSERT INTO issues (id, key, project_key, summary, issue_type, is_subtask, "
            "status, status_category, priority, assignee_id, reporter_id, created, "
            "updated, resolved, due_date, story_points, labels, components, "
            "fix_versions, parent_key, parent_issue_type) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET key = EXCLUDED.key, "
            "project_key = EXCLUDED.project_key, summary = EXCLUDED.summary, "
            "issue_type = EXCLUDED.issue_type, is_subtask = EXCLUDED.is_subtask, "
            "status = EXCLUDED.status, status_category = EXCLUDED.status_category, "
            "priority = EXCLUDED.priority, assignee_id = EXCLUDED.assignee_id, "
            "reporter_id = EXCLUDED.reporter_id, created = EXCLUDED.created, "
            "updated = EXCLUDED.updated, resolved = EXCLUDED.resolved, "
            "due_date = EXCLUDED.due_date, story_points = EXCLUDED.story_points, "
            "labels = EXCLUDED.labels, components = EXCLUDED.components, "
            "fix_versions = EXCLUDED.fix_versions, parent_key = EXCLUDED.parent_key, "
            "parent_issue_type = EXCLUDED.parent_issue_type",
            rows.issues,
        )
        for table in ("status_changes", "issue_sprints", "worklogs"):
            cur.execute(
                pgsql.SQL("DELETE FROM {} WHERE issue_id = ANY(%s)").format(
                    pgsql.Identifier(table)
                ),
                (ids,),
            )
        if rows.status_changes:
            cur.executemany(
                "INSERT INTO status_changes (issue_id, issue_key, changed_at, author_id, "
                "from_status, to_status, from_category, to_category) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                rows.status_changes,
            )
        if rows.issue_sprints:
            cur.executemany(
                "INSERT INTO issue_sprints (issue_id, sprint_id) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                rows.issue_sprints,
            )
        if rows.worklogs:
            cur.executemany(
                "INSERT INTO worklogs (id, issue_id, issue_key, author_id, started, "
                "time_spent_seconds) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET issue_id = EXCLUDED.issue_id, "
                "issue_key = EXCLUDED.issue_key, author_id = EXCLUDED.author_id, "
                "started = EXCLUDED.started, "
                "time_spent_seconds = EXCLUDED.time_spent_seconds",
                list(rows.worklogs.values()),
            )


def _counts(conn: psycopg.Connection, schema: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for name, _ in jschema.TABLES:
        if name.startswith("_"):
            continue
        out[name] = conn.execute(
            pgsql.SQL("SELECT count(*) FROM {}").format(pgsql.Identifier(schema, name))
        ).fetchone()[0]
    return out


# --- the run -----------------------------------------------------------------


def run_sync(
    cfg: JiraSourceConfig,
    *,
    full: bool = False,
    on_event: OnEvent | None = None,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SyncResult:
    """Sync one source. Raises SyncInProgressError, JiraError, or a DB error.

    ``transport``/``sleep`` exist for tests (``httpx.MockTransport``, no waits).
    """
    emit = on_event or (lambda kind, data: None)
    started = time.monotonic()

    with JiraClient(
        cfg.site, cfg.email, cfg.api_token, transport=transport, sleep=sleep
    ) as client, syncstore.admin_connection(settings) as conn, syncstore.source_lock(
        conn, cfg.schema
    ):
        me = client.myself() or {}
        rebuilt = ensure_tables(conn, cfg.schema, cfg.site)
        full = full or rebuilt or cfg.cursor is None

        fmap = FieldMap.discover(client.fields())
        categories = {
            str(s.get("id")): (s.get("statusCategory") or {}).get("name")
            for s in (client.statuses() or [])
        }
        jql = build_jql(cfg.scope_jql, None if full else cfg.cursor, me.get("timeZone"))
        emit("start", {"full": full, "jql": jql, "rebuilt": rebuilt})

        seen: set[int] = set()
        max_updated = cfg.cursor
        synced = 0
        for page in client.search(jql, fields=fmap.request_fields()):
            rows = parse_page(page, fmap, categories, client)
            write_page(conn, cfg.schema, rows)
            seen.update(rows.issue_ids)
            synced += len(rows.issues)
            if rows.max_updated and (max_updated is None or rows.max_updated > max_updated):
                max_updated = rows.max_updated
            emit("page", {"issues": synced})

        deleted = 0
        if full:
            with conn.transaction():
                deleted = conn.execute(
                    pgsql.SQL("DELETE FROM {} WHERE NOT (id = ANY(%s))").format(
                        pgsql.Identifier(cfg.schema, "issues")
                    ),
                    (list(seen),),
                ).rowcount
                # Dimension rows only exist because an issue referenced them.
                # After a scope change a full sync is the only point that can
                # tell, and a project nobody's issues are in would otherwise
                # keep showing up in "issues by project" as a zero bar.
                conn.execute(pgsql.SQL("SET LOCAL search_path = {}").format(
                    pgsql.Identifier(cfg.schema)
                ))
                conn.execute(
                    "DELETE FROM projects p WHERE NOT EXISTS "
                    "(SELECT 1 FROM issues i WHERE i.project_key = p.key)"
                )
                conn.execute(
                    "DELETE FROM sprints s WHERE NOT EXISTS "
                    "(SELECT 1 FROM issue_sprints x WHERE x.sprint_id = s.id)"
                )
                conn.execute(
                    "DELETE FROM users u WHERE NOT EXISTS (SELECT 1 FROM issues i "
                    "WHERE u.account_id IN (i.assignee_id, i.reporter_id)) "
                    "AND NOT EXISTS (SELECT 1 FROM status_changes c "
                    "WHERE c.author_id = u.account_id) "
                    "AND NOT EXISTS (SELECT 1 FROM worklogs w "
                    "WHERE w.author_id = u.account_id)"
                )
        with conn.transaction():
            conn.execute(
                pgsql.SQL("UPDATE {} SET synced_at = now(), site = %s").format(
                    pgsql.Identifier(cfg.schema, "_sync_meta")
                ),
                (cfg.site,),
            )
        stats = {
            "full": full,
            "issues_synced": synced,
            "issues_deleted": deleted,
            "seconds": round(time.monotonic() - started, 1),
            "tables": _counts(conn, cfg.schema),
        }

    emit("done", stats)
    return SyncResult(cursor=max_updated, full=full, stats=stats)

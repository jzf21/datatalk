"""A fake Jira Cloud, as an ``httpx.MockTransport``.

Just enough of the REST API for the sync: ``/myself``, ``/field``, ``/status``,
token-paginated ``/search/jql`` (honouring the ``updated >=`` bound the sync
writes, so incremental runs are really incremental), the offset-paginated
changelog/worklog follow-ups, and ``approximate-count``. It records every
request so a test can assert what was asked, and can be told to 429 first.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx

STORY_POINTS = "customfield_10016"
SPRINT = "customfield_10020"

STATUSES = [
    {"id": "1", "name": "To Do", "statusCategory": {"name": "To Do"}},
    {"id": "3", "name": "In Progress", "statusCategory": {"name": "In Progress"}},
    {"id": "10001", "name": "Done", "statusCategory": {"name": "Done"}},
]

_UPDATED_RE = re.compile(r'updated >= "(\d{4}/\d{2}/\d{2} \d{2}:\d{2})"')


def ts(s: str) -> str:
    """``2024-03-01 10:00`` → Jira's ``2024-03-01T10:00:00.000+0000``."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M").strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def user(aid: str, name: str | None = None) -> dict[str, Any]:
    return {
        "accountId": aid,
        "displayName": name or aid.title(),
        "active": True,
        "accountType": "atlassian",
        "emailAddress": f"{aid}@example.com",  # must NOT be synced
    }


def make_issue(
    iid: int,
    key: str,
    *,
    updated: str,
    created: str | None = None,
    status: str = "To Do",
    category: str = "To Do",
    points: float | None = None,
    assignee: str | None = "alice",
    sprints: list[dict[str, Any]] | None = None,
    transitions: list[tuple[str, str, str, str]] = (),
    worklogs: list[tuple[int, str, str, int]] = (),
    history: list[tuple[str, str, list[dict[str, Any]]]] = (),
    labels: list[str] = (),
    parent: tuple[str, str] | None = None,
    changelog_total: int | None = None,
    worklog_total: int | None = None,
) -> dict[str, Any]:
    """One issue in search-result shape.

    ``transitions`` are ``(when, from_id, to_id, author)``; ``worklogs`` are
    ``(id, started, author, seconds)``. ``history`` is extra changelog entries,
    ``(when, author, items)`` -- build items with :func:`sprint_change`,
    :func:`points_change`. A ``*_total`` larger than the list makes the inline
    list look truncated, forcing the follow-up fetch.
    """
    names = {s["id"]: s["name"] for s in STATUSES}
    histories = [
        {
            "id": str(n),
            "created": ts(when),
            "author": user(author),
            "items": [
                {"field": "status", "fieldId": "status", "from": f, "to": t,
                 "fromString": names[f], "toString": names[t]},
                {"field": "assignee", "fieldId": "assignee", "from": None, "to": "x"},
            ],
        }
        for n, (when, f, t, author) in enumerate(transitions)
    ] + [
        {"id": str(100 + n), "created": ts(when), "author": user(author), "items": items}
        for n, (when, author, items) in enumerate(history)
    ]
    logs = [
        {"id": str(wid), "started": ts(started), "author": user(author),
         "timeSpentSeconds": secs}
        for wid, started, author, secs in worklogs
    ]
    project = key.split("-")[0]
    return {
        "id": str(iid),
        "key": key,
        "fields": {
            "summary": f"Issue {key}",
            "project": {"id": f"p-{project}", "key": project, "name": f"Project {project}",
                        "projectTypeKey": "software"},
            "issuetype": {"name": "Story", "subtask": False},
            "status": {"name": status, "statusCategory": {"name": category}},
            "priority": {"name": "Medium"},
            "assignee": user(assignee) if assignee else None,
            "reporter": user("rita"),
            "created": ts(created or updated),
            "updated": ts(updated),
            "resolutiondate": ts(updated) if category == "Done" else None,
            "duedate": None,
            "labels": list(labels),
            "components": [{"name": "api"}],
            "fixVersions": [],
            "parent": (
                {"key": parent[0], "fields": {"issuetype": {"name": parent[1]}}}
                if parent else None
            ),
            STORY_POINTS: points,
            SPRINT: sprints,
            "worklog": {"worklogs": logs, "total": worklog_total or len(logs),
                        "maxResults": 20},
        },
        "changelog": {"histories": histories, "total": changelog_total or len(histories),
                      "maxResults": 40},
    }


def sprint_change(before: list[int], after: list[int]) -> dict[str, Any]:
    """A Sprint changelog item: ids as Jira writes them, ``"12, 13"``."""
    return {
        "field": "Sprint", "fieldtype": "custom", "fieldId": SPRINT,
        "from": ", ".join(map(str, before)) or None,
        "fromString": ", ".join(f"Sprint {i}" for i in before) or None,
        "to": ", ".join(map(str, after)) or None,
        "toString": ", ".join(f"Sprint {i}" for i in after) or None,
    }


def points_change(before: float | None, after: float | None) -> dict[str, Any]:
    return {
        "field": "Story point estimate", "fieldtype": "custom", "fieldId": STORY_POINTS,
        "from": None, "fromString": None if before is None else str(before),
        "to": None, "toString": None if after is None else str(after),
    }


def sprint(sid: int, state: str, start: str | None = None, end: str | None = None,
           complete: str | None = None, board: int | None = None) -> dict[str, Any]:
    """A sprint object as the sprint custom field carries it."""
    out: dict[str, Any] = {"id": sid, "name": f"Sprint {sid}", "state": state}
    if start:
        out["startDate"] = ts(start)
    if end:
        out["endDate"] = ts(end)
    if complete:
        out["completeDate"] = ts(complete)
    if board is not None:
        out["boardId"] = board
    return out


class FakeJira:
    def __init__(self, issues: list[dict[str, Any]] | None = None, *, page_size: int = 2):
        self.issues = list(issues or [])
        self.page_size = page_size
        self.requests: list[httpx.Request] = []
        self.rate_limit_first = 0
        self.extra_changelog: dict[str, list[dict[str, Any]]] = {}
        self.extra_worklogs: dict[str, list[dict[str, Any]]] = {}
        self.status_code_override: int | None = None
        # The Agile API. None = the site has no Jira Software (404s).
        self.boards: list[dict[str, Any]] | None = [
            {"id": 1, "name": "ABC board", "type": "scrum", "location": {"projectKey": "ABC"}},
        ]
        self.board_sprints: dict[int, list[dict[str, Any]]] = {}
        self.agile_status: int | None = None

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def searches(self) -> list[str]:
        return [
            r.url.params["jql"] for r in self.requests
            if r.url.path == "/rest/api/3/search/jql" and "nextPageToken" not in r.url.params
        ]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.rate_limit_first > 0:
            self.rate_limit_first -= 1
            return httpx.Response(429, headers={"Retry-After": "1"})
        if self.status_code_override:
            return httpx.Response(self.status_code_override, json={"errorMessages": ["nope"]})
        path = request.url.path
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "me", "displayName": "Sync Bot",
                                             "timeZone": "UTC"})
        if path == "/rest/api/3/field":
            return httpx.Response(200, json=[
                {"id": "summary", "name": "Summary", "schema": {"type": "string"}},
                {"id": STORY_POINTS, "name": "Story point estimate",
                 "schema": {"type": "number", "custom": "x:float"}},
                {"id": SPRINT, "name": "Sprint",
                 "schema": {"type": "array", "custom": "com.pyxis.greenhopper.jira:gh-sprint"}},
            ])
        if path == "/rest/api/3/status":
            return httpx.Response(200, json=STATUSES)
        if path == "/rest/api/3/search/approximate-count":
            return httpx.Response(200, json={"count": len(self.issues)})
        if path == "/rest/api/3/search/jql":
            return self._search(request)
        if path.startswith("/rest/agile/1.0/"):
            return self._agile(request, path)
        m = re.match(r"^/rest/api/3/issue/(\d+)/(changelog|worklog)$", path)
        if m:
            iid, kind = m.groups()
            items = (self.extra_changelog if kind == "changelog" else self.extra_worklogs).get(iid, [])
            key = "values" if kind == "changelog" else "worklogs"
            start = int(request.url.params.get("startAt", 0))
            chunk = items[start:start + 1]  # one per page: exercises pagination
            return httpx.Response(200, json={key: chunk, "startAt": start,
                                             "total": len(items),
                                             "isLast": start + len(chunk) >= len(items)})
        return httpx.Response(404, json={"errorMessages": [f"no route {path}"]})

    def _agile(self, request: httpx.Request, path: str) -> httpx.Response:
        if self.agile_status:
            return httpx.Response(self.agile_status, json={"errorMessages": ["no"]})
        if self.boards is None:
            return httpx.Response(404, json={"errorMessages": ["no Jira Software"]})
        if path == "/rest/agile/1.0/board":
            items = self.boards
        else:
            m = re.match(r"^/rest/agile/1.0/board/(\d+)/sprint$", path)
            if not m:
                return httpx.Response(404, json={"errorMessages": [f"no route {path}"]})
            items = self.board_sprints.get(int(m.group(1)), [])
        start = int(request.url.params.get("startAt", 0))
        chunk = items[start:start + 1]
        return httpx.Response(200, json={"values": chunk, "startAt": start,
                                         "total": len(items),
                                         "isLast": start + len(chunk) >= len(items)})

    def _search(self, request: httpx.Request) -> httpx.Response:
        jql = request.url.params["jql"]
        m = _UPDATED_RE.search(jql)
        since = (
            datetime.strptime(m.group(1), "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
            if m else None
        )
        matching = [
            i for i in self.issues
            if since is None
            or datetime.strptime(i["fields"]["updated"], "%Y-%m-%dT%H:%M:%S.000%z") >= since
        ]
        matching.sort(key=lambda i: i["fields"]["updated"])
        start = int(request.url.params.get("nextPageToken") or 0)
        page = matching[start:start + self.page_size]
        nxt = start + self.page_size
        body: dict[str, Any] = {"issues": json.loads(json.dumps(page)),
                                "isLast": nxt >= len(matching)}
        if nxt < len(matching):
            body["nextPageToken"] = str(nxt)
        return httpx.Response(200, json=body)

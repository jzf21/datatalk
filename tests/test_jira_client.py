"""The Jira client and the pure half of the sync: no database, no network.

The host allowlist is the SSRF guard for a server that makes authenticated
requests to whatever an admin types, so it gets the most cases. The parser is
where a Jira payload becomes what the agent reads, so a field silently dropped
or mis-mapped here is a chart that is silently wrong.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from datatalk.integrations.jira.client import (
    JiraClient,
    JiraError,
    JiraHostNotAllowedError,
    normalize_site,
)
from datatalk.integrations.jira.sync import FieldMap, build_jql, parse_page, parse_ts
from tests.jira_fake import SPRINT, STATUSES, STORY_POINTS, FakeJira, make_issue

ALLOWED = [".atlassian.net"]

# --- host allowlist ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("acme.atlassian.net", "acme.atlassian.net"),
        ("https://Acme.atlassian.net/", "acme.atlassian.net"),
        ("https://acme.atlassian.net/jira/software/projects/ABC", "acme.atlassian.net"),
        ("  acme-eu.atlassian.net ", "acme-eu.atlassian.net"),
    ],
)
def test_a_pasted_site_is_reduced_to_its_hostname(raw, expected):
    assert normalize_site(raw, ALLOWED) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "atlassian.net",  # the suffix alone is not a site
        ".atlassian.net",
        "evil.com",
        "acme.atlassian.net.evil.com",
        "evilatlassian.net",  # suffix must match at a label boundary
        "localhost",
        "127.0.0.1",
        "169.254.169.254",
        "acme.atlassian.net:8080",
        "user@acme.atlassian.net",
        "http://user:pw@acme.atlassian.net",
        "acme.atlassian.net\n.evil.com",
    ],
)
def test_anything_off_the_allowlist_is_refused(raw):
    with pytest.raises(JiraHostNotAllowedError):
        normalize_site(raw, ALLOWED)


def test_the_allowlist_is_configurable_for_data_center():
    assert normalize_site("jira.corp.example", [".corp.example"]) == "jira.corp.example"
    with pytest.raises(JiraHostNotAllowedError):
        normalize_site("acme.atlassian.net", [".corp.example"])


# --- transport ---------------------------------------------------------------


def _client(fake: FakeJira, sleeps: list[float] | None = None) -> JiraClient:
    return JiraClient(
        "acme.atlassian.net",
        "bot@example.com",
        "token",
        transport=fake.transport,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )


def test_429_is_retried_after_the_advertised_delay():
    fake = FakeJira()
    fake.rate_limit_first = 2
    sleeps: list[float] = []
    with _client(fake, sleeps) as jc:
        assert jc.myself()["displayName"] == "Sync Bot"
    assert sleeps == [1.0, 1.0]


def test_endless_429_fails_instead_of_hammering():
    fake = FakeJira()
    fake.rate_limit_first = 99
    with _client(fake) as jc, pytest.raises(JiraError):
        jc.myself()
    assert len(fake.requests) == 5


def test_a_redirect_is_refused_not_followed():
    """An allowed host must not be able to bounce the request elsewhere."""

    def handler(request):
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    jc = JiraClient("acme.atlassian.net", "e", "t", transport=httpx.MockTransport(handler))
    with pytest.raises(JiraError, match="redirect"):
        jc.myself()


def test_bad_credentials_say_so():
    fake = FakeJira()
    fake.status_code_override = 401
    with _client(fake) as jc, pytest.raises(JiraError, match="401"):
        jc.myself()


def test_basic_auth_is_email_and_token():
    fake = FakeJira()
    with _client(fake) as jc:
        jc.myself()
    auth = fake.requests[0].headers["authorization"]
    assert auth.startswith("Basic ")


def test_search_follows_next_page_tokens():
    fake = FakeJira(
        [make_issue(i, f"ABC-{i}", updated=f"2024-03-0{i} 10:00") for i in range(1, 6)],
        page_size=2,
    )
    with _client(fake) as jc:
        pages = list(jc.search('updated >= "1970/01/01 00:00"', fields=["summary"]))
    assert [len(p) for p in pages] == [2, 2, 1]


# --- JQL ---------------------------------------------------------------------


def test_incremental_jql_overlaps_the_cursor_in_the_users_timezone():
    cursor = datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
    jql = build_jql("project = ABC", cursor, "Asia/Kolkata")  # UTC+05:30
    assert jql == (
        '(project = ABC) AND updated >= "2024/03/01 17:28" ORDER BY updated ASC'
    )


def test_a_full_sync_is_still_bounded():
    """The enhanced search refuses unbounded JQL, even with no scope."""
    assert build_jql(None, None, "UTC") == (
        'updated >= "1970/01/01 00:00" ORDER BY updated ASC'
    )


def test_the_scopes_own_order_by_is_dropped():
    """Ours must win, or the cursor stops meaning 'everything before is done'."""
    jql = build_jql("project = ABC order by created DESC", None, "UTC")
    assert jql.count("ORDER BY") == 1
    assert "created DESC" not in jql
    assert jql.startswith("(project = ABC)")


def test_an_unknown_timezone_falls_back_to_utc():
    cursor = datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
    assert '"2024/03/01 11:58"' in build_jql(None, cursor, "Mars/Olympus")


# --- parsing -----------------------------------------------------------------


def test_jira_timestamps_parse():
    assert parse_ts("2024-01-02T10:11:12.000+0000") == datetime(
        2024, 1, 2, 10, 11, 12, tzinfo=timezone.utc
    )
    assert parse_ts(None) is None
    assert parse_ts("yesterday") is None


def test_field_discovery_finds_story_points_and_sprint_by_meaning():
    fmap = FieldMap.discover(
        [
            {"id": "customfield_1", "name": "Story Points", "schema": {}},
            {"id": "customfield_2", "name": "Sprint",
             "schema": {"custom": "com.pyxis.greenhopper.jira:gh-sprint"}},
        ]
    )
    assert fmap == FieldMap(story_points="customfield_1", sprint="customfield_2")
    assert FieldMap.discover([]) == FieldMap(None, None)


def _categories():
    return {s["id"]: s["statusCategory"]["name"] for s in STATUSES}


def test_an_issue_becomes_rows_the_agent_can_chart():
    sprint = {"id": 7, "boardId": 3, "name": "Sprint 7", "state": "closed",
              "startDate": "2024-02-19T09:00:00.000Z", "endDate": "2024-03-04T09:00:00.000Z",
              "completeDate": "2024-03-04T10:00:00.000Z", "goal": "ship"}
    issue = make_issue(
        10, "ABC-10", created="2024-02-20 09:00", updated="2024-03-01 10:00",
        status="Done", category="Done", points=5, labels=["backend"],
        sprints=[sprint], parent=("ABC-1", "Epic"),
        transitions=[("2024-02-21 09:00", "1", "3", "alice"),
                     ("2024-03-01 10:00", "3", "10001", "bob")],
        worklogs=[(99, "2024-02-22 09:00", "alice", 3600)],
    )
    rows = parse_page([issue], FieldMap(STORY_POINTS, SPRINT), _categories())

    (row,) = rows.issues
    assert row[0] == 10 and row[1] == "ABC-10" and row[2] == "ABC"
    assert row[6:8] == ("Done", "Done")  # status, status_category
    assert row[15] == 5.0  # story_points
    assert row[16] == ["backend"] and row[17] == ["api"]
    assert row[19:21] == ("ABC-1", "Epic")
    assert row[13] is not None  # resolved

    # Only the *status* items of the changelog, with categories resolved by id.
    assert [(c[4], c[5], c[6], c[7]) for c in rows.status_changes] == [
        ("To Do", "In Progress", "To Do", "In Progress"),
        ("In Progress", "Done", "In Progress", "Done"),
    ]
    assert rows.issue_sprints == [(10, 7)]
    assert rows.sprints[7][2:4] == ("Sprint 7", "closed")
    assert list(rows.worklogs.values()) == [
        (99, 10, "ABC-10", "alice", parse_ts("2024-02-22T09:00:00.000+0000"), 3600)
    ]
    assert rows.max_updated == parse_ts("2024-03-01T10:00:00.000+0000")


def test_users_are_synced_without_their_email():
    rows = parse_page(
        [make_issue(1, "ABC-1", updated="2024-03-01 10:00")],
        FieldMap(STORY_POINTS, SPRINT),
        _categories(),
    )
    assert rows.users["alice"] == ("alice", "Alice", True, "atlassian")
    assert not any("@" in str(v) for u in rows.users.values() for v in u)


def test_an_unestimated_issue_has_null_points_not_zero():
    rows = parse_page(
        [make_issue(1, "ABC-1", updated="2024-03-01 10:00", points=None)],
        FieldMap(STORY_POINTS, SPRINT),
        _categories(),
    )
    assert rows.issues[0][15] is None


def test_truncated_changelogs_and_worklogs_are_fetched_in_full():
    fake = FakeJira()
    fake.extra_changelog["5"] = [
        {"id": str(n), "created": "2024-03-0%dT10:00:00.000+0000" % (n + 1),
         "author": {"accountId": "bob"},
         "items": [{"field": "status", "from": "1", "to": "3",
                    "fromString": "To Do", "toString": "In Progress"}]}
        for n in range(3)
    ]
    fake.extra_worklogs["5"] = [
        {"id": str(100 + n), "started": "2024-03-01T10:00:00.000+0000",
         "author": {"accountId": "bob"}, "timeSpentSeconds": 60}
        for n in range(3)
    ]
    issue = make_issue(5, "ABC-5", updated="2024-03-05 10:00",
                       changelog_total=3, worklog_total=3)
    with _client(fake) as jc:
        rows = parse_page([issue], FieldMap(STORY_POINTS, SPRINT), _categories(), jc)
    assert len(rows.status_changes) == 3
    assert len(rows.worklogs) == 3


# --- sync store configuration ------------------------------------------------


def test_the_sync_store_may_not_be_the_app_database():
    from datatalk.integrations import syncstore
    from tests.conftest import make_settings

    app = "postgresql+psycopg://u:p@db.internal:5432/datatalk"
    same = make_settings(DATABASE_URL=app, DATATALK_SYNC_DATABASE_URL=app.replace("u:p", "x:y"))
    other = make_settings(
        DATABASE_URL=app, DATATALK_SYNC_DATABASE_URL=app.replace("/datatalk", "/datatalk_sync")
    )
    unset = make_settings(DATABASE_URL=app, DATATALK_SYNC_DATABASE_URL="")

    assert syncstore.is_configured(same) is False  # different creds, same database
    assert syncstore.is_configured(other) is True
    assert syncstore.is_configured(unset) is False
    with pytest.raises(syncstore.SyncStoreNotConfiguredError, match="app database"):
        syncstore.endpoint(same)

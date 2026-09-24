"""The shape of a synced Jira source, as the agent will read it.

The comments are not decoration. ``PostgresWarehouse`` reads
``obj_description``/``col_description`` into the catalog and into
``describe_source``, so every sentence here reaches the Analyst's prompt. They
say what the data *means* -- which table answers "cycle time", which NULL means
"unestimated" -- which is the thing a model lacks far more often than it lacks
column names.

Bump :data:`SCHEMA_VERSION` whenever the DDL changes. A store whose
``_sync_meta.schema_version`` differs is dropped and fully resynced rather than
migrated: it is a cache of Jira, and Jira is the source of truth.
"""

from __future__ import annotations

SCHEMA_VERSION = 1

# Order matters: children reference issues(id).
TABLES: tuple[tuple[str, str], ...] = (
    (
        "_sync_meta",
        """
        CREATE TABLE _sync_meta (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            schema_version integer NOT NULL,
            site text NOT NULL,
            synced_at timestamptz
        )
        """,
    ),
    (
        "projects",
        """
        CREATE TABLE projects (
            id text PRIMARY KEY,
            key text NOT NULL UNIQUE,
            name text NOT NULL,
            project_type text
        )
        """,
    ),
    (
        "users",
        """
        CREATE TABLE users (
            account_id text PRIMARY KEY,
            display_name text,
            active boolean,
            account_type text
        )
        """,
    ),
    (
        "issues",
        """
        CREATE TABLE issues (
            id bigint PRIMARY KEY,
            key text NOT NULL UNIQUE,
            project_key text NOT NULL,
            summary text,
            issue_type text,
            is_subtask boolean NOT NULL DEFAULT false,
            status text,
            status_category text,
            priority text,
            assignee_id text,
            reporter_id text,
            created timestamptz NOT NULL,
            updated timestamptz NOT NULL,
            resolved timestamptz,
            due_date date,
            story_points numeric,
            labels text[] NOT NULL DEFAULT '{}',
            components text[] NOT NULL DEFAULT '{}',
            fix_versions text[] NOT NULL DEFAULT '{}',
            parent_key text,
            parent_issue_type text
        )
        """,
    ),
    (
        "status_changes",
        """
        CREATE TABLE status_changes (
            issue_id bigint NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            issue_key text NOT NULL,
            changed_at timestamptz NOT NULL,
            author_id text,
            from_status text,
            to_status text,
            from_category text,
            to_category text
        )
        """,
    ),
    (
        "sprints",
        """
        CREATE TABLE sprints (
            id bigint PRIMARY KEY,
            board_id bigint,
            name text,
            state text,
            start_date timestamptz,
            end_date timestamptz,
            complete_date timestamptz,
            goal text
        )
        """,
    ),
    (
        "issue_sprints",
        """
        CREATE TABLE issue_sprints (
            issue_id bigint NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            sprint_id bigint NOT NULL,
            PRIMARY KEY (issue_id, sprint_id)
        )
        """,
    ),
    (
        "worklogs",
        """
        CREATE TABLE worklogs (
            id bigint PRIMARY KEY,
            issue_id bigint NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            issue_key text NOT NULL,
            author_id text,
            started timestamptz NOT NULL,
            time_spent_seconds integer NOT NULL
        )
        """,
    ),
)

INDEXES: tuple[str, ...] = (
    "CREATE INDEX ON issues (project_key, updated)",
    "CREATE INDEX ON issues (resolved)",
    "CREATE INDEX ON status_changes (issue_id, changed_at)",
    "CREATE INDEX ON status_changes (changed_at)",
    "CREATE INDEX ON worklogs (issue_id)",
    "CREATE INDEX ON worklogs (started)",
)

TABLE_COMMENTS: dict[str, str] = {
    "_sync_meta": "Sync bookkeeping. synced_at is when this copy of Jira was last refreshed.",
    "projects": "Jira projects that have at least one synced issue.",
    "users": "Jira accounts seen as assignee, reporter or worklog/changelog author. No emails.",
    "issues": (
        "One row per Jira issue in scope, current state only. For history "
        "(cycle time, throughput, time in status) use status_changes."
    ),
    "status_changes": (
        "One row per status transition, from the issue changelog. Cycle time = "
        "first transition into category 'In Progress' to the last into 'Done'."
    ),
    "sprints": "Scrum sprints any synced issue has been in.",
    "issue_sprints": (
        "Issue-to-sprint membership. An issue carried over appears in several sprints."
    ),
    "worklogs": "Time logged against issues.",
}

COLUMN_COMMENTS: dict[str, dict[str, str]] = {
    "issues": {
        "key": "Human key, e.g. ABC-123. Stable and what users refer to.",
        "status_category": (
            "One of 'To Do', 'In Progress', 'Done'. Prefer this over status for "
            "open/closed questions -- status names differ per workflow."
        ),
        "resolved": "When the issue was resolved; NULL while unresolved.",
        "story_points": "Estimate in story points; NULL when unestimated (not zero).",
        "labels": "Array of labels; filter with 'label' = ANY(labels).",
        "components": "Array of component names.",
        "fix_versions": "Array of fix version names.",
        "parent_key": "Parent issue key: the epic for a story, the story for a sub-task.",
        "parent_issue_type": "Issue type of the parent, e.g. 'Epic'.",
        "assignee_id": "users.account_id; NULL when unassigned.",
        "reporter_id": "users.account_id.",
    },
    "status_changes": {
        "from_category": "Status category before the change: 'To Do', 'In Progress' or 'Done'.",
        "to_category": "Status category after the change.",
        "author_id": "users.account_id of whoever made the change.",
    },
    "sprints": {
        "state": "One of 'future', 'active', 'closed'.",
        "complete_date": "When the sprint was closed; NULL unless state = 'closed'.",
    },
    "worklogs": {
        "started": "When the logged work started (not when it was logged).",
        "time_spent_seconds": "Duration logged. Divide by 3600.0 for hours.",
        "author_id": "users.account_id of whoever logged the time.",
    },
}

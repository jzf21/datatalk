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

SCHEMA_VERSION = 2

# Order matters: children reference issues(id).
TABLES: tuple[tuple[str, str], ...] = (
    (
        "_sync_meta",
        """
        CREATE TABLE _sync_meta (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            schema_version integer NOT NULL,
            site text NOT NULL,
            synced_at timestamptz,
            time_zone text
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
        "boards",
        """
        CREATE TABLE boards (
            id bigint PRIMARY KEY,
            name text,
            board_type text,
            project_key text
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
        "sprint_events",
        """
        CREATE TABLE sprint_events (
            issue_id bigint NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            issue_key text NOT NULL,
            sprint_id bigint NOT NULL,
            changed_at timestamptz NOT NULL,
            author_id text,
            action text NOT NULL CHECK (action IN ('added', 'removed'))
        )
        """,
    ),
    (
        "field_changes",
        """
        CREATE TABLE field_changes (
            issue_id bigint NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
            issue_key text NOT NULL,
            changed_at timestamptz NOT NULL,
            author_id text,
            field text NOT NULL,
            from_value text,
            to_value text
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
    "CREATE INDEX ON sprint_events (sprint_id, changed_at)",
    "CREATE INDEX ON sprint_events (issue_id)",
    "CREATE INDEX ON field_changes (issue_id, field, changed_at)",
    "CREATE INDEX ON worklogs (issue_id)",
    "CREATE INDEX ON worklogs (started)",
)

TABLE_COMMENTS: dict[str, str] = {
    "_sync_meta": (
        "Sync bookkeeping. synced_at is when this copy of Jira was last "
        "refreshed; time_zone is the Jira site's zone, for bucketing by day."
    ),
    "boards": (
        "Jira Software boards. Empty when the site has no Jira Software or the "
        "account cannot see boards; sprints.board_id still links to it."
    ),
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
    "sprint_events": (
        "When an issue entered or left a sprint, from the changelog. An issue "
        "created straight into a sprint gets an 'added' row at its created time. "
        "Sprint scope at time T = issues whose latest event at or before T is "
        "'added'. Use this, not issue_sprints, for burndown and scope change."
    ),
    "field_changes": (
        "Changes to story_points, assignee, priority and issue_type, from the "
        "changelog. Use it for the value a field had at a past time."
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
        "board_id": "boards.id of the board the sprint belongs to.",
        "complete_date": "When the sprint was closed; NULL unless state = 'closed'.",
    },
    "sprint_events": {
        "action": "'added' (issue entered the sprint) or 'removed' (it left).",
        "author_id": "users.account_id of whoever made the change; NULL for creation.",
    },
    "field_changes": {
        "field": "One of 'story_points', 'assignee', 'priority', 'issue_type'.",
        "from_value": (
            "Value before the change; NULL when unset. story_points values are "
            "numeric text (cast with ::numeric); assignee values are account ids."
        ),
        "to_value": "Value after the change; NULL when cleared.",
    },
    "worklogs": {
        "started": "When the logged work started (not when it was logged).",
        "time_spent_seconds": "Duration logged. Divide by 3600.0 for hours.",
        "author_id": "users.account_id of whoever logged the time.",
    },
}

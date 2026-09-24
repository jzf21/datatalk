"""Jira, as the agent sees it: a read-only Postgres schema.

Jira is not queried live. A sync job (:mod:`datatalk.integrations.jira`) copies
it into one schema of the sync store, and this adapter is the ordinary Postgres
adapter pointed at that schema as a login role that can read it and nothing
else. So a Jira-sourced chart goes through exactly the same ``run_sql`` →
``materialize()`` → refresh path as any other; there is no second rendering
path to disagree with the first.

What differs is only the dialect's ``prompt_hint``, which tells the model what
the synced tables mean, and a distinct ``type`` so the fingerprint -- and hence
the client and schema cache -- can never collide with a real Postgres source.
"""

from __future__ import annotations

from dataclasses import replace

from datatalk.warehouse.postgres import POSTGRES_DIALECT, PostgresWarehouse

JIRA_DIALECT = replace(
    POSTGRES_DIALECT,
    name="jira",
    prompt_hint=(
        POSTGRES_DIALECT.prompt_hint
        + " Jira sources are a synced PostgreSQL copy of Jira: issues (one row "
        "per issue), status_changes (one row per status transition -- use it "
        "for cycle time and throughput), sprints / issue_sprints, worklogs, "
        "users and projects. Join on issue_key and account_id. Data is as fresh "
        "as the last sync, not live."
    ),
)


class JiraWarehouse(PostgresWarehouse):
    """The Postgres adapter, labelled as Jira. Read-only by the same means."""

    dialect = JIRA_DIALECT

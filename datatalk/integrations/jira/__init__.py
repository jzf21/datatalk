"""Jira Cloud as a synced source. See ``docs/jira.md``.

``client`` talks to Jira, ``sync`` copies it into the sync store, ``service``
records the outcome in the app database, and ``schema`` is the table shape the
agent reads -- comments included, since they reach its prompt.
"""

"""Sources that are *synced* rather than queried live.

A non-SQL system (Jira) is copied into the sync store -- a separate Postgres
database, one schema and one read-only login role per source -- and from then
on it is an ordinary SQL source to everything in ``agent/``, ``dashboards/``
and ``warehouse/``. This package is the only code that talks to those systems
or writes to the sync store.
"""

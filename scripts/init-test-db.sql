-- Runs once, on first container start, via docker-entrypoint-initdb.d.
-- Creates the separate database the test suite migrates and truncates.
CREATE DATABASE datatalk_test OWNER datatalk;

-- The evaluation fixture (see datatalk/evals/). Two databases, because the
-- suite measures whether the agent routes each statement to the right *source*
-- and one database could only ever be one source.
--
-- `datatalk-eval seed` creates these itself if they are absent, which is the
-- path that actually runs for anyone whose Postgres volume predates this file:
-- docker-entrypoint-initdb.d is skipped entirely on an existing volume, so a
-- setup step that lived only here would silently no-op for every existing
-- developer.
CREATE DATABASE datatalk_eval_sales OWNER datatalk;
CREATE DATABASE datatalk_eval_events OWNER datatalk;

-- The sync store: synced sources (Jira) live here, one schema and one
-- read-only login role per source -- never in the app database. The test
-- suite derives `datatalk_test_sync` from DATATALK_TEST_DATABASE_URL and
-- creates it on demand, so only the dev store is created here. Existing
-- volumes skip this file; create it by hand (see docs/jira.md).
CREATE DATABASE datatalk_sync OWNER datatalk;

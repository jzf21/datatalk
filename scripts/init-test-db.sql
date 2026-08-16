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

-- Runs once, on first container start, via docker-entrypoint-initdb.d.
-- Creates the separate database the test suite migrates and truncates.
CREATE DATABASE datatalk_test OWNER datatalk;

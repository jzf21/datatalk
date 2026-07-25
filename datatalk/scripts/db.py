"""Database migration CLI.

    datatalk-db upgrade      # apply migrations
    datatalk-db current      # show the applied revision
    datatalk-db history
    datatalk-db revision -m "add widgets"
    datatalk-db downgrade -1

Wraps Alembic's Python API so the URL comes from Settings and nobody has to
edit alembic.ini. Migrations are deliberately NOT run automatically at startup:
two uvicorn workers racing `upgrade head` can corrupt alembic_version.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from datatalk.config import get_settings

_ROOT = Path(__file__).resolve().parents[2]


def alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    resolved = url or get_settings().database_url
    if not resolved:
        raise SystemExit(
            "DATABASE_URL is not set.\n"
            "  1. docker compose up -d\n"
            "  2. cp .env.example .env   (then check DATABASE_URL)"
        )
    cfg.set_main_option("sqlalchemy.url", resolved)
    return cfg


def head_revision(cfg: Config | None = None) -> str | None:
    return ScriptDirectory.from_config(cfg or alembic_config()).get_current_head()


def current_revision(engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def schema_is_current(engine, cfg: Config | None = None) -> bool:
    """True when the database is migrated to the newest revision."""
    return current_revision(engine) == head_revision(cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="datatalk-db", description=__doc__)
    parser.add_argument("--url", help="override DATABASE_URL")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("upgrade").add_argument("revision", nargs="?", default="head")
    sub.add_parser("downgrade").add_argument("revision", nargs="?", default="-1")
    sub.add_parser("current")
    sub.add_parser("history")
    rev = sub.add_parser("revision")
    rev.add_argument("-m", "--message", required=True)
    rev.add_argument("--autogenerate", action="store_true")

    args = parser.parse_args(argv)
    cfg = alembic_config(args.url)

    if args.cmd == "upgrade":
        command.upgrade(cfg, args.revision)
    elif args.cmd == "downgrade":
        command.downgrade(cfg, args.revision)
    elif args.cmd == "current":
        command.current(cfg, verbose=True)
    elif args.cmd == "history":
        command.history(cfg, verbose=True)
    elif args.cmd == "revision":
        command.revision(cfg, message=args.message, autogenerate=args.autogenerate)
    return 0


if __name__ == "__main__":
    sys.exit(main())

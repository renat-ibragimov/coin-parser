"""Minimal, ORM-free Postgres connection helper shared by any step that
writes to coin_keeper's production database. No SQLAlchemy/Alembic --
plain psycopg (v3), parameterized queries, one transaction per run.
"""

from __future__ import annotations

import os

import psycopg


def get_database_url() -> str:
    """DATABASE_URL from the environment. Getting a connection to the
    right database (e.g. an ssh tunnel to the prod server) is the
    caller's responsibility, not this code's -- it only reads the env
    var and fails clearly if it's missing."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Point it at coin_keeper's database "
            "(e.g. via an ssh tunnel to the prod server) before running this step."
        )
    return url


def existing_columns(conn: psycopg.Connection, table: str) -> set[str]:
    """Column names actually present on `table`, via information_schema
    -- used as a guard before writing, so a schema drift between this
    repo and coin_keeper fails loudly instead of writing partial data."""
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %(table)s",
        {"table": table},
    ).fetchall()
    return {row[0] for row in rows}

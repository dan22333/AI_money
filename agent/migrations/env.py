"""Alembic environment.

Builds the DB URL from config.settings (PG_*) at runtime so no credentials live
in alembic.ini. Supports both the Cloud SQL unix socket (PG_HOST=/cloudsql/...,
used on Cloud Run) and a TCP host (PG_HOST=127.0.0.1 via the Cloud SQL Auth
Proxy, used by the deploy migrate job).

We own ONLY the extension + any of our own tables here. mem0 creates and manages
its own vector table — Alembic deliberately does not touch it.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings  # noqa: E402

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = None  # raw DDL migrations; no ORM models to autogenerate


def _url() -> str:
    pw = settings.PG_PASSWORD
    if settings.PG_HOST.startswith("/"):  # unix socket (Cloud Run)
        return (f"postgresql+psycopg2://{settings.PG_USER}:{pw}@/{settings.PG_DB}"
                f"?host={settings.PG_HOST}")
    return (f"postgresql+psycopg2://{settings.PG_USER}:{pw}@{settings.PG_HOST}:5432/"
            f"{settings.PG_DB}")


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

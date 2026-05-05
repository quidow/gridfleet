import asyncio
from logging.config import fileConfig

from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.models  # noqa: F401 — register all models on Base.metadata
from alembic import context
from app.config import settings
from app.database import Base

config = context.config

_externally_supplied_connection = config.attributes.get("connection")
_externally_supplied_url = config.attributes.get("sqlalchemy.url")
if _externally_supplied_connection is None and _externally_supplied_url is None:
    config.set_main_option("sqlalchemy.url", settings.database_url)
elif _externally_supplied_url is not None:
    config.set_main_option("sqlalchemy.url", _externally_supplied_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"}
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    target_search_path = config.attributes.get("target_search_path")
    if target_search_path is not None:
        connection.execute(text(f'SET search_path TO "{target_search_path}"'))
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    if _externally_supplied_connection is not None:
        await _externally_supplied_connection.run_sync(do_run_migrations)
        return

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    if isinstance(_externally_supplied_connection, Connection):
        do_run_migrations(_externally_supplied_connection)
        return
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

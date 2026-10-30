import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.db.session import build_session_factory


@pytest.fixture(params=["sqlite"] + (["postgresql"] if os.getenv("TEST_DATABASE_URL") else []))
def database(request, tmp_path):
    """Migrate a fresh SQLite file or a unique schema in an opted-in PostgreSQL DB."""
    backend = request.param
    schema = "test_" + uuid4().hex
    if backend == "postgresql":
        url = os.environ["TEST_DATABASE_URL"]
        if not url.startswith("postgresql+asyncpg://"):
            raise ValueError("TEST_DATABASE_URL must use postgresql+asyncpg")
        engine = create_async_engine(
            url,
            poolclass=NullPool,
            connect_args={"server_settings": {"search_path": schema + ",public"}},
        )
    else:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'reviews.db'}", poolclass=NullPool
        )

        @event.listens_for(engine.sync_engine, "connect")
        def sqlite_connection(connection, record):
            connection.isolation_level = None
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(engine.sync_engine, "begin")
        def sqlite_begin(connection):
            connection.exec_driver_sql("BEGIN")

    def upgrade(connection):
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        config.attributes["schema"] = schema if backend == "postgresql" else None
        command.upgrade(config, "head")

    async def initialize():
        async with engine.begin() as connection:
            if backend == "postgresql":
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                # The schema did not exist during the dialect's initial connect.
                # Pin reflection/version tracking so public tables cannot be mistaken
                # for this test's migrations when public is visible for vector types.
                connection.dialect.default_schema_name = schema
            await connection.run_sync(upgrade)

    async def cleanup():
        try:
            if backend == "postgresql":
                async with engine.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await engine.dispose()

    try:
        asyncio.run(initialize())
        yield SimpleNamespace(
            engine=engine, sessions=build_session_factory(engine), backend=backend, schema=schema
        )
    finally:
        asyncio.run(cleanup())

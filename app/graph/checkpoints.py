from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import sql
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.core.exceptions import WorkflowPersistenceError


def checkpoint_connection_string(settings: Settings) -> str:
    url = make_url(str(settings.database_url)).set(drivername="postgresql")
    # Driver conversion is explicit; credentials are never printed or checkpointed.
    url = url.update_query_dict({"connect_timeout": "10"})
    return url.render_as_string(hide_password=False)


@asynccontextmanager
async def open_postgres_checkpointer(
    settings: Settings, *, schema: str = "review_workflow"
) -> AsyncIterator[AsyncPostgresSaver]:
    try:
        async with await psycopg.AsyncConnection.connect(
            checkpoint_connection_string(settings),
            autocommit=True,
            row_factory=dict_row,
            prepare_threshold=0,
        ) as connection:
            await connection.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema))
            )
            await connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(schema))
            )
            saver = AsyncPostgresSaver(
                connection,
                serde=JsonPlusSerializer(allowed_json_modules=[], allowed_msgpack_modules=[]),
            )
            await saver.setup()
            yield saver
    except psycopg.Error:
        raise WorkflowPersistenceError("PostgreSQL checkpoint operation failed") from None

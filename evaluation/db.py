"""Throwaway PostgreSQL schemas for evaluations and tests, so real data is never touched."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from app.config import Settings
from app.db.migrate import apply_migrations


class PgVectorUnavailable(RuntimeError):
    """The server cannot provide the pgvector extension to this database."""


@asynccontextmanager
async def isolated_pool() -> AsyncIterator[asyncpg.Pool]:
    """Yield a pool bound to a fresh schema with every migration applied, then drop it.

    Every table exists in the throwaway schema, so it shadows the real ones in `public`;
    `public` stays on the search_path only so the extension's types resolve.
    """
    kwargs = Settings().postgres_connect_kwargs
    admin = await asyncpg.connect(timeout=5, **kwargs)
    schema = f"test_{uuid.uuid4().hex}"
    pool: asyncpg.Pool | None = None
    try:
        try:
            await admin.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
        except (asyncpg.FeatureNotSupportedError, asyncpg.InsufficientPrivilegeError) as error:
            raise PgVectorUnavailable(str(error)) from error
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(
            min_size=1,
            max_size=4,
            server_settings={"search_path": f'"{schema}", public'},
            **kwargs,
        )
        async with pool.acquire() as conn:
            await apply_migrations(conn)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()

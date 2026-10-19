import asyncio

import asyncpg

from app.config import Settings

_pool: asyncpg.Pool | None = None
_lock = asyncio.Lock()


async def get_pool(settings: Settings) -> asyncpg.Pool:
    """Return the process-wide pool, creating it on first use."""
    global _pool
    async with _lock:
        if _pool is None:
            _pool = await asyncpg.create_pool(
                min_size=1, max_size=10, timeout=10, **settings.postgres_connect_kwargs
            )
        return _pool


async def close_pool() -> None:
    global _pool
    async with _lock:
        if _pool is not None:
            await _pool.close()
            _pool = None

import asyncpg
from fastapi import Depends

from app.config import Settings, get_settings
from app.db.pool import get_pool


async def get_db_pool(settings: Settings = Depends(get_settings)) -> asyncpg.Pool:
    return await get_pool(settings)

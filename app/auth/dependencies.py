import asyncpg
from fastapi import Depends

from app.auth.users import PostgresUserRepository, UserRepository
from app.db.dependencies import get_db_pool


def get_user_repository(pool: asyncpg.Pool = Depends(get_db_pool)) -> UserRepository:
    return PostgresUserRepository(pool)

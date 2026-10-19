from datetime import UTC, datetime
from typing import Protocol

import asyncpg
from fastapi import Depends

from app.db.dependencies import get_db_pool


class TokenRevocations(Protocol):
    async def is_revoked(self, jti: str, user_id: str, issued_at: int) -> bool: ...

    async def revoke(self, jti: str, expires_at: int) -> None: ...


class PostgresTokenRevocations:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def is_revoked(self, jti: str, user_id: str, issued_at: int) -> bool:
        """Revoked if signed out, or the user is gone/deactivated, or a password reset
        happened after the token was issued. One query so every request pays one round trip."""
        return bool(
            await self._pool.fetchval(
                "SELECT EXISTS (SELECT 1 FROM revoked_tokens WHERE jti = $1)"
                " OR NOT EXISTS (SELECT 1 FROM users WHERE id = $2::uuid AND is_active"
                " AND (tokens_valid_after IS NULL"
                " OR $3 >= date_trunc('second', tokens_valid_after)))",
                jti,
                user_id,
                datetime.fromtimestamp(issued_at, UTC),
            )
        )

    async def revoke(self, jti: str, expires_at: int) -> None:
        await self._pool.execute("DELETE FROM revoked_tokens WHERE expires_at < now()")
        await self._pool.execute(
            "INSERT INTO revoked_tokens (jti, expires_at) VALUES ($1, $2)"
            " ON CONFLICT (jti) DO NOTHING",
            jti,
            datetime.fromtimestamp(expires_at, UTC),
        )


def get_token_revocations(pool: asyncpg.Pool = Depends(get_db_pool)) -> TokenRevocations:
    return PostgresTokenRevocations(pool)

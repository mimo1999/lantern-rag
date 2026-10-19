from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import asyncpg


@dataclass(frozen=True, slots=True)
class StoredUser:
    id: str
    tenant_id: str
    username: str
    password_hash: str
    is_active: bool
    roles: tuple[str, ...]
    is_locked: bool = False


class UserRepository(Protocol):
    async def find(self, tenant_id: str, username: str) -> StoredUser | None: ...

    async def record_failed_login(self, user_id: str, max_attempts: int) -> bool:
        """Count a failed attempt; returns True when the account is (now) locked."""
        ...

    async def record_successful_login(self, user_id: str) -> bool:
        """Reset the failure count; returns False if the account is locked."""
        ...

    async def find_active_id_by_email(self, tenant_id: str, email: str) -> str | None: ...

    async def create_password_reset(
        self, user_id: str, token_hash: str, expires_at: datetime
    ) -> bool:
        """Store a reset token; False if one was already issued in the last minute."""
        ...

    async def complete_password_reset(self, token_hash: str, password_hash: str) -> bool:
        """Single-use: set the password, unlock the account, revoke earlier tokens."""
        ...


class PostgresUserRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def find(self, tenant_id: str, username: str) -> StoredUser | None:
        row = await self._pool.fetchrow(
            "SELECT u.id::text AS id, u.tenant_id, u.username, u.password_hash, u.is_active,"
            " u.locked_at IS NOT NULL AS is_locked,"
            " COALESCE(array_agg(r.role ORDER BY r.role) FILTER (WHERE r.role IS NOT NULL),"
            " ARRAY[]::text[]) AS roles"
            " FROM users u LEFT JOIN user_roles r ON r.user_id = u.id"
            " WHERE u.tenant_id = $1 AND u.username = $2 GROUP BY u.id",
            tenant_id,
            username,
        )
        if row is None:
            return None
        return StoredUser(
            id=row["id"],
            tenant_id=row["tenant_id"],
            username=row["username"],
            password_hash=row["password_hash"],
            is_active=row["is_active"],
            roles=tuple(row["roles"]),
            is_locked=row["is_locked"],
        )

    async def record_failed_login(self, user_id: str, max_attempts: int) -> bool:
        # One atomic UPDATE so parallel guesses cannot read a stale counter.
        return bool(
            await self._pool.fetchval(
                "UPDATE users SET failed_logins = failed_logins + 1,"
                " locked_at = CASE WHEN failed_logins + 1 >= $2 THEN COALESCE(locked_at, now())"
                " ELSE locked_at END WHERE id = $1::uuid RETURNING locked_at IS NOT NULL",
                user_id,
                max_attempts,
            )
        )

    async def record_successful_login(self, user_id: str) -> bool:
        row = await self._pool.fetchval(
            "UPDATE users SET failed_logins = 0 WHERE id = $1::uuid AND locked_at IS NULL"
            " RETURNING id",
            user_id,
        )
        return row is not None

    async def find_active_id_by_email(self, tenant_id: str, email: str) -> str | None:
        return await self._pool.fetchval(
            "SELECT id::text FROM users WHERE tenant_id = $1 AND lower(email) = lower($2)"
            " AND is_active",
            tenant_id,
            email,
        )

    async def create_password_reset(
        self, user_id: str, token_hash: str, expires_at: datetime
    ) -> bool:
        inserted = await self._pool.fetchval(
            "INSERT INTO password_resets (token_hash, user_id, expires_at)"
            " SELECT $1, $2::uuid, $3 WHERE NOT EXISTS (SELECT 1 FROM password_resets"
            " WHERE user_id = $2::uuid AND created_at > now() - interval '1 minute')"
            " RETURNING 1",
            token_hash,
            user_id,
            expires_at,
        )
        return inserted is not None

    async def complete_password_reset(self, token_hash: str, password_hash: str) -> bool:
        async with self._pool.acquire() as conn, conn.transaction():
            user_id = await conn.fetchval(
                "UPDATE password_resets SET used_at = now() WHERE token_hash = $1"
                " AND used_at IS NULL AND expires_at > now() RETURNING user_id",
                token_hash,
            )
            if user_id is None:
                return False
            await conn.execute(
                "UPDATE users SET password_hash = $2, failed_logins = 0, locked_at = NULL,"
                " tokens_valid_after = now() WHERE id = $1",
                user_id,
                password_hash,
            )
            return True

import asyncpg
import pytest

from app.auth.users import PostgresUserRepository


@pytest.mark.asyncio
async def test_find_returns_user_with_sorted_roles_and_scopes_by_tenant(
    pg_pool: asyncpg.Pool,
) -> None:
    await pg_pool.execute("INSERT INTO tenants (id, name) VALUES ('acme', 'Acme'), ('globex', 'G')")
    user_id = await pg_pool.fetchval(
        "INSERT INTO users (tenant_id, username, password_hash) VALUES ('acme', 'bob', 'h')"
        " RETURNING id"
    )
    await pg_pool.execute(
        "INSERT INTO user_roles (user_id, role) VALUES ($1, 'finance'), ($1, 'employee')", user_id
    )
    repo = PostgresUserRepository(pg_pool)

    found = await repo.find("acme", "bob")

    assert found is not None
    assert found.roles == ("employee", "finance")
    assert await repo.find("globex", "bob") is None


@pytest.mark.asyncio
async def test_find_returns_user_without_roles(pg_pool: asyncpg.Pool) -> None:
    await pg_pool.execute("INSERT INTO tenants (id, name) VALUES ('acme', 'Acme')")
    await pg_pool.execute(
        "INSERT INTO users (tenant_id, username, password_hash) VALUES ('acme', 'bob', 'h')"
    )

    found = await PostgresUserRepository(pg_pool).find("acme", "bob")

    assert found is not None and found.roles == ()

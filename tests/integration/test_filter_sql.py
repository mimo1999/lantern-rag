"""Runs the real authorization predicate in PostgreSQL against a plain table (no pgvector
needed), so the security-critical WHERE semantics are verified on any database."""

import asyncpg
import pytest
import pytest_asyncio

from app.auth.models import UserContext
from app.retrieval.filters import build_authorization_filter

ROWS = [
    ("default-open", "default", None, ["employee", "admin"]),
    ("default-admin", "default", None, ["admin"]),
    ("acme-eng", "tenant", "acme", ["employee", "admin"]),
    ("acme-fin", "tenant", "acme", ["finance", "admin"]),
    ("acme-exec", "tenant", "acme", ["admin"]),
    ("globex-eng", "tenant", "globex", ["employee", "admin"]),
    ("globex-fin", "tenant", "globex", ["admin"]),
]


async def visible(pool: asyncpg.Pool, tenant: str, *roles: str, extra: str = "") -> set[str]:
    clause, params = build_authorization_filter(UserContext("u", tenant, roles)).to_sql(1)
    rows = await pool.fetch(f"SELECT name FROM probe WHERE {clause} {extra}", *params)
    return {row["name"] for row in rows}


@pytest_asyncio.fixture
async def probe(pg_pool: asyncpg.Pool) -> asyncpg.Pool:
    await pg_pool.execute(
        "CREATE TABLE probe (name TEXT, scope TEXT, tenant_id TEXT, allowed_roles TEXT[])"
    )
    await pg_pool.executemany("INSERT INTO probe VALUES ($1, $2, $3, $4)", ROWS)
    return pg_pool


@pytest.mark.asyncio
async def test_employee_sees_shared_and_own_tenant_documents_only(probe: asyncpg.Pool) -> None:
    assert await visible(probe, "acme", "employee") == {"default-open", "acme-eng"}
    assert await visible(probe, "globex", "employee") == {"default-open", "globex-eng"}


@pytest.mark.asyncio
async def test_admin_never_crosses_tenants(probe: asyncpg.Pool) -> None:
    assert await visible(probe, "acme", "admin") == {
        "default-open",
        "default-admin",
        "acme-eng",
        "acme-fin",
        "acme-exec",
    }


@pytest.mark.asyncio
async def test_role_intersection_uses_any_of_the_callers_roles(probe: asyncpg.Pool) -> None:
    assert await visible(probe, "acme", "employee", "finance") == {
        "default-open",
        "acme-eng",
        "acme-fin",
    }


@pytest.mark.asyncio
async def test_unknown_tenant_and_empty_roles_see_nothing_private(probe: asyncpg.Pool) -> None:
    assert await visible(probe, "initech", "employee") == {"default-open"}
    assert await visible(probe, "acme") == set()


@pytest.mark.asyncio
async def test_hostile_values_are_inert(probe: asyncpg.Pool) -> None:
    assert await visible(probe, "acme' OR '1'='1", "employee") == {"default-open"}
    assert await visible(probe, "acme", "employee' OR '1'='1") == set()
    assert await probe.fetchval("SELECT count(*) FROM probe") == len(ROWS)


@pytest.mark.asyncio
async def test_filter_composes_with_additional_conditions(probe: asyncpg.Pool) -> None:
    clause, params = build_authorization_filter(UserContext("u", "acme", ("admin",))).to_sql(2)
    rows = await probe.fetch(
        f"SELECT name FROM probe WHERE name LIKE $1 AND {clause}", "acme-%", *params
    )

    assert {row["name"] for row in rows} == {"acme-eng", "acme-fin", "acme-exec"}

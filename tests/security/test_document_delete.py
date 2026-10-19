from uuid import NAMESPACE_URL, uuid5

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.ingestion.dependencies import get_document_registry
from app.ingestion.registry import PostgresDocumentRegistry
from app.main import app
from evaluation.testing import VectorEnv


def doc_id(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, name))


@pytest_asyncio.fixture
async def setup(vector_env: VectorEnv, test_settings):
    await vector_env.add("handbook", "Shared rules.", ("admin",), scope="default")
    await vector_env.add("acme-plan", "Acme plan " * 300, ("admin",), tenant_id="acme")
    await vector_env.add("globex-plan", "Globex plan.", ("admin",), tenant_id="globex")
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_document_registry] = lambda: PostgresDocumentRegistry(
        vector_env.pool
    )
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test"), vector_env.pool
    app.dependency_overrides.clear()


async def chunks(pool: asyncpg.Pool, name: str) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM document_chunks WHERE document_id = $1::uuid", doc_id(name)
    )


async def delete(client: AsyncClient, token: str, name: str):
    return await client.delete(
        f"/documents/{doc_id(name)}", headers={"Authorization": f"Bearer {token}"}
    )


@pytest.mark.asyncio
async def test_tenant_admin_deletes_own_document_and_its_vectors(setup, token_for) -> None:
    client, pool = setup
    assert await chunks(pool, "acme-plan") > 1

    response = await delete(client, token_for("alice", "acme", ("admin",)), "acme-plan")

    assert response.status_code == 204
    assert await chunks(pool, "acme-plan") == 0
    registry_rows = await pool.fetchval(
        "SELECT count(*) FROM documents WHERE id = $1::uuid", doc_id("acme-plan")
    )
    assert registry_rows == 0
    assert await chunks(pool, "globex-plan") == 1  # neighbours untouched


@pytest.mark.asyncio
async def test_cannot_delete_another_tenants_or_a_shared_document(setup, token_for) -> None:
    client, pool = setup
    alice = token_for("alice", "acme", ("admin",))

    other_tenant = await delete(client, alice, "globex-plan")
    shared = await delete(client, alice, "handbook")

    assert other_tenant.status_code == shared.status_code == 404  # indistinguishable from absent
    assert await chunks(pool, "globex-plan") == 1
    assert await chunks(pool, "handbook") == 1


@pytest.mark.asyncio
async def test_platform_admin_deletes_shared_but_not_other_tenants(setup, token_for) -> None:
    client, pool = setup
    root = token_for("root", "platform", ("platform-admin", "admin"))

    assert (await delete(client, root, "handbook")).status_code == 204
    assert (await delete(client, root, "globex-plan")).status_code == 404
    assert await chunks(pool, "handbook") == 0
    assert await chunks(pool, "globex-plan") == 1


@pytest.mark.asyncio
async def test_non_admin_cannot_delete(setup, token_for) -> None:
    client, pool = setup

    response = await delete(client, token_for("bob", "acme", ("employee",)), "acme-plan")

    assert response.status_code == 403
    assert await chunks(pool, "acme-plan") > 1

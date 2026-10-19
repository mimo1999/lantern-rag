import asyncpg
import pytest

from app.ingestion.registry import PostgresDocumentRegistry, RegisteredDocument

ID_1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ID_2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def document(document_id: str, scope: str, tenant_id: str | None) -> RegisteredDocument:
    return RegisteredDocument(
        document_id, scope, tenant_id, "Title", None, "sum", ("admin",), "indexing", 0, "alice"
    )


@pytest.mark.asyncio
async def test_lifecycle_and_duplicate_detection_is_scoped(pg_pool: asyncpg.Pool) -> None:
    await pg_pool.execute("INSERT INTO tenants (id, name) VALUES ('acme', 'A'), ('globex', 'G')")
    registry = PostgresDocumentRegistry(pg_pool)

    await registry.create(document(ID_1, "tenant", "acme"))
    assert await registry.find_ready_duplicate("tenant", "acme", "sum") is None  # still indexing

    await registry.mark_ready(ID_1, 4)
    found = await registry.find_ready_duplicate("tenant", "acme", "sum")

    assert found is not None and (found.id, found.chunk_count, found.status) == (ID_1, 4, "ready")
    assert await registry.find_ready_duplicate("tenant", "globex", "sum") is None
    assert await registry.find_ready_duplicate("default", None, "sum") is None


@pytest.mark.asyncio
async def test_default_scope_duplicates_match_on_null_tenant(pg_pool: asyncpg.Pool) -> None:
    registry = PostgresDocumentRegistry(pg_pool)
    await registry.create(document(ID_2, "default", None))
    await registry.mark_ready(ID_2, 2)

    found = await registry.find_ready_duplicate("default", None, "sum")

    assert found is not None and found.id == ID_2


@pytest.mark.asyncio
async def test_failed_documents_are_not_treated_as_duplicates(pg_pool: asyncpg.Pool) -> None:
    await pg_pool.execute("INSERT INTO tenants (id, name) VALUES ('acme', 'A')")
    registry = PostgresDocumentRegistry(pg_pool)
    await registry.create(document(ID_1, "tenant", "acme"))
    await registry.mark_failed(ID_1)

    assert await registry.find_ready_duplicate("tenant", "acme", "sum") is None

import pytest

from app.ingestion.indexer import DocumentForIndexing, DocumentIndexer
from app.retrieval.embeddings import HashingEmbedder


class FakeVectorStore:
    def __init__(self) -> None:
        self.embedder = HashingEmbedder(8)
        self.records = []

    async def upsert_chunks(self, records: list) -> None:
        self.records.extend(records)


@pytest.mark.asyncio
async def test_indexer_attaches_acl_payload_to_every_chunk() -> None:
    retriever = FakeVectorStore()
    indexer = DocumentIndexer(retriever)  # type: ignore[arg-type]

    count = await indexer.index(
        DocumentForIndexing(
            document_id="doc-123",
            scope="tenant",
            tenant_id="acme",
            title="Finance Report",
            text="Quarterly revenue was 81.4 million euros.",
            allowed_roles=("admin", "finance"),
        )
    )

    assert count.chunks == 1
    assert retriever.records[0].scope == "tenant"
    assert retriever.records[0].tenant_id == "acme"
    assert retriever.records[0].allowed_roles == ("admin", "finance")


@pytest.mark.asyncio
async def test_indexer_attaches_null_tenant_to_default_scope_chunks() -> None:
    retriever = FakeVectorStore()
    indexer = DocumentIndexer(retriever)  # type: ignore[arg-type]

    await indexer.index(
        DocumentForIndexing(
            document_id="doc-handbook",
            scope="default",
            title="Employee Handbook",
            text="This handbook applies to every tenant.",
            allowed_roles=("admin", "employee"),
        )
    )

    assert retriever.records[0].scope == "default"
    assert retriever.records[0].tenant_id is None


def test_tenant_scope_requires_tenant_id() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        DocumentForIndexing(
            document_id="doc-123",
            scope="tenant",
            title="Finance Report",
            text="text",
            allowed_roles=("admin",),
        )


def test_default_scope_rejects_tenant_id() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        DocumentForIndexing(
            document_id="doc-handbook",
            scope="default",
            tenant_id="acme",
            title="Employee Handbook",
            text="text",
            allowed_roles=("admin",),
        )

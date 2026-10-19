import pytest

from app.ingestion.indexer import DocumentForIndexing, IndexResult
from app.ingestion.registry import RegisteredDocument
from app.ingestion.service import IngestionService, NewDocument, content_checksum


class FakeRegistry:
    def __init__(self, duplicate: RegisteredDocument | None = None) -> None:
        self.duplicate = duplicate
        self.created: list[RegisteredDocument] = []
        self.events: list[tuple[str, str]] = []
        self.errors: list[str | None] = []

    async def create(self, document: RegisteredDocument) -> None:
        self.created.append(document)
        self.events.append(("created", document.id))

    async def update_checksum(self, document_id: str, checksum: str) -> None:
        self.events.append(("checksum", document_id))

    async def mark_ready(self, document_id: str, chunk_count: int, *counts: int) -> None:
        self.events.append(("ready", document_id))

    async def mark_failed(self, document_id: str, error: str | None = None) -> None:
        self.events.append(("failed", document_id))
        self.errors.append(error)

    async def mark_duplicate(self, document_id: str, duplicate_of: str) -> None:
        self.events.append(("duplicate", duplicate_of))

    async def find_ready_duplicate(self, scope, tenant_id, checksum):
        return self.duplicate


class FakeIndexer:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.indexed: list[DocumentForIndexing] = []

    async def index(self, document: DocumentForIndexing) -> IndexResult:
        if self.error:
            raise self.error
        self.indexed.append(document)
        return IndexResult(3)


def new_document(**overrides) -> NewDocument:
    fields = dict(
        scope="tenant",
        tenant_id="acme",
        title="Report",
        text="some text",
        allowed_roles=("admin",),
        created_by="alice",
    )
    return NewDocument(**{**fields, **overrides})


@pytest.mark.asyncio
async def test_ingest_registers_indexes_and_marks_ready() -> None:
    registry, indexer = FakeRegistry(), FakeIndexer()

    result = await IngestionService(registry, indexer).ingest(new_document())

    assert (result.chunks_indexed, result.duplicate) == (3, False)
    assert [event for event, _ in registry.events] == ["created", "ready"]
    assert registry.created[0].status == "indexing" and registry.created[0].created_by == "alice"
    assert indexer.indexed[0].document_id == result.document_id


@pytest.mark.asyncio
async def test_identical_content_is_not_indexed_twice() -> None:
    existing = RegisteredDocument(
        "existing-id", "tenant", "acme", "Report", None, "c", ("admin",), "ready", 7, "alice"
    )
    registry, indexer = FakeRegistry(duplicate=existing), FakeIndexer()

    result = await IngestionService(registry, indexer).ingest(new_document())

    assert (result.document_id, result.chunks_indexed, result.duplicate) == ("existing-id", 7, True)
    assert registry.created == [] and indexer.indexed == []


@pytest.mark.asyncio
async def test_indexing_failure_marks_document_failed_and_propagates() -> None:
    registry = FakeRegistry()

    with pytest.raises(RuntimeError, match="vector store down"):
        await IngestionService(registry, FakeIndexer(RuntimeError("vector store down"))).ingest(
            new_document()
        )

    assert [event for event, _ in registry.events] == ["created", "failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides", [{"tenant_id": None}, {"scope": "default", "tenant_id": "acme"}]
)
async def test_invalid_scope_pairing_is_rejected_before_anything_is_written(overrides) -> None:
    registry = FakeRegistry()

    with pytest.raises(ValueError, match="tenant_id"):
        await IngestionService(registry, FakeIndexer()).ingest(new_document(**overrides))

    assert registry.events == []


def test_checksum_ignores_whitespace_and_role_order_but_not_role_set() -> None:
    base = content_checksum("a  b\n c", ("admin", "finance"))

    assert content_checksum("a b c", ("finance", "admin")) == base
    assert content_checksum("a b c", ("admin",)) != base
    assert content_checksum("a b d", ("admin", "finance")) != base


@pytest.mark.asyncio
async def test_start_registers_a_pending_row_before_the_file_is_parsed() -> None:
    registry = FakeRegistry()

    document_id = await IngestionService(registry, FakeIndexer()).start(
        scope="tenant",
        tenant_id="acme",
        title="Report",
        source_uri="report.pdf",
        allowed_roles=("admin",),
        created_by="alice",
    )

    [row] = registry.created
    assert (
        row.id == document_id and row.status == "indexing" and row.checksum.startswith("pending:")
    )
    assert registry.events == [("created", document_id)]


@pytest.mark.asyncio
async def test_start_rejects_a_bad_scope_pairing_before_writing() -> None:
    registry = FakeRegistry()

    with pytest.raises(ValueError, match="tenant_id"):
        await IngestionService(registry, FakeIndexer()).start(
            scope="tenant",
            tenant_id=None,
            title="T",
            source_uri=None,
            allowed_roles=("admin",),
            created_by="alice",
        )

    assert registry.events == []


@pytest.mark.asyncio
async def test_complete_indexes_the_registered_row_and_marks_it_ready() -> None:
    registry, indexer = FakeRegistry(), FakeIndexer()

    result = await IngestionService(registry, indexer).complete("doc-1", new_document())

    assert (result.document_id, result.chunks_indexed, result.duplicate) == ("doc-1", 3, False)
    assert [event for event, _ in registry.events] == ["checksum", "ready"]
    assert indexer.indexed[0].document_id == "doc-1"


@pytest.mark.asyncio
async def test_complete_marks_identical_content_as_a_duplicate_without_indexing() -> None:
    existing = RegisteredDocument(
        "existing-id", "tenant", "acme", "Report", None, "c", ("admin",), "ready", 7, "alice"
    )
    registry, indexer = FakeRegistry(duplicate=existing), FakeIndexer()

    result = await IngestionService(registry, indexer).complete("doc-2", new_document())

    assert (result.document_id, result.duplicate) == ("existing-id", True)
    assert registry.events == [("duplicate", "existing-id")] and indexer.indexed == []


@pytest.mark.asyncio
async def test_failures_record_a_message_that_never_contains_exception_text() -> None:
    from app.retrieval.embeddings import EmbeddingError

    for error, expected in [
        (RuntimeError("password=hunter2 host=10.0.0.5"), "Indexing failed. Upload the file again."),
        (
            EmbeddingError("http://ollama:11434 refused"),
            "The embedding service was unavailable. Upload the file again.",
        ),
        (ValueError("Document contains no indexable text"), "Document contains no indexable text"),
    ]:
        registry = FakeRegistry()
        with pytest.raises(type(error)):
            await IngestionService(registry, FakeIndexer(error)).complete("doc-3", new_document())
        assert registry.errors == [expected]

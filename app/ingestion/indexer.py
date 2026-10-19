from dataclasses import dataclass
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from app.ingestion.chunker import chunk_text
from app.retrieval.filters import DEFAULT_SCOPE, TENANT_SCOPE
from app.retrieval.pgvector import ChunkRecord, PgVectorRetriever

DocumentScope = Literal["default", "tenant"]


@dataclass(frozen=True, slots=True)
class Asset:
    """A table (as markdown) or an image (as its caption), already text, indexed with the
    asset embedding model."""

    kind: Literal["table", "image"]
    text: str
    page: int | None = None


@dataclass(frozen=True, slots=True)
class IndexResult:
    chunks: int
    tables: int = 0
    images: int = 0

    @property
    def total(self) -> int:
        return self.chunks + self.tables + self.images


@dataclass(frozen=True, slots=True)
class DocumentForIndexing:
    document_id: str
    scope: DocumentScope
    title: str
    text: str
    allowed_roles: tuple[str, ...]
    tenant_id: str | None = None
    source_uri: str | None = None
    assets: tuple[Asset, ...] = ()

    def __post_init__(self) -> None:
        if self.scope == TENANT_SCOPE and not self.tenant_id:
            raise ValueError("tenant-scoped documents require a tenant_id")
        if self.scope == DEFAULT_SCOPE and self.tenant_id is not None:
            raise ValueError("default-scoped documents must not have a tenant_id")


class DocumentIndexer:
    def __init__(
        self,
        retriever: PgVectorRetriever,
        chunk_size: int = 1_200,
        chunk_overlap: int = 150,
        assets: PgVectorRetriever | None = None,
    ) -> None:
        self._retriever = retriever
        self._assets = assets
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap

    async def index(self, document: DocumentForIndexing) -> IndexResult:
        chunks = chunk_text(document.text, self._chunk_size, self._chunk_overlap)
        if not chunks and not document.assets:
            raise ValueError("Document contains no indexable text")
        if document.assets and self._assets is None:
            raise RuntimeError("document has tables or images but no asset index is configured")
        # Embed everything before writing anything, so an embedding failure leaves no rows.
        embeddings = await self._retriever.embedder.embed_many([chunk.text for chunk in chunks])
        records = [
            self._record(
                document,
                f"{document.document_id}:{chunk.index}",
                f"{document.document_id}_{chunk.index:04d}",
                chunk.text,
                embedding,
            )
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ]
        if not document.assets:
            await self._retriever.upsert_chunks(records)
            return IndexResult(len(records))

        assert self._assets is not None
        asset_embeddings = await self._assets.embedder.embed_many(
            [asset.text for asset in document.assets]
        )
        asset_records = [
            self._record(
                document,
                f"{document.document_id}:asset:{number}",
                f"{document.document_id}_a{number:04d}",
                asset.text,
                embedding,
                kind=asset.kind,
                page=asset.page,
            )
            for number, (asset, embedding) in enumerate(
                zip(document.assets, asset_embeddings, strict=True)
            )
        ]
        # Both tables in one transaction: a document is either fully indexed or not at all.
        async with self._retriever.pool.acquire() as conn, conn.transaction():
            await self._retriever.upsert_chunks(records, conn)
            await self._assets.upsert_chunks(asset_records, conn)
        return IndexResult(
            len(records),
            tables=sum(a.kind == "table" for a in document.assets),
            images=sum(a.kind == "image" for a in document.assets),
        )

    @staticmethod
    def _record(
        document: DocumentForIndexing,
        key: str,
        chunk_id: str,
        content: str,
        embedding: list[float],
        *,
        kind: str = "text",
        page: int | None = None,
    ) -> ChunkRecord:
        return ChunkRecord(
            id=str(uuid5(NAMESPACE_URL, key)),
            document_id=document.document_id,
            chunk_id=chunk_id,
            scope=document.scope,
            tenant_id=document.tenant_id,
            allowed_roles=document.allowed_roles,
            title=document.title,
            source_uri=document.source_uri,
            content=content,
            embedding=embedding,
            kind=kind,
            page=page,
        )

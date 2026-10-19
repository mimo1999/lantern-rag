"""Test-support helper: a pgvector retrieval stack over a throwaway schema."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from uuid import NAMESPACE_URL, uuid5

import asyncpg

from app.ingestion.indexer import Asset, DocumentForIndexing, DocumentIndexer
from app.retrieval.combined import CombinedRetriever
from app.retrieval.embeddings import HashingEmbedder
from app.retrieval.pgvector import PgVectorRetriever
from app.retrieval.service import RetrievalService, RetrievedChunk
from evaluation.corpus import EVAL_DIMENSION, register_and_index

ASSET_DIMENSION = 1024  # matches the vector(1024) column of asset_chunks


@dataclass
class VectorEnv:
    """Readable document names on top of the UUID ids the database requires."""

    pool: asyncpg.Pool
    retriever: PgVectorRetriever
    indexer: DocumentIndexer
    service: RetrievalService
    # Prose plus table/image chunks, searched in both tables like production.
    combined: RetrievalService
    _names: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(cls, pool: asyncpg.Pool, top_k: int = 10) -> "VectorEnv":
        retriever = PgVectorRetriever(pool, HashingEmbedder(EVAL_DIMENSION))
        assets = PgVectorRetriever(pool, HashingEmbedder(ASSET_DIMENSION), "asset_chunks")
        combined = RetrievalService(CombinedRetriever(retriever, assets, 3), top_k)
        return cls(
            pool,
            retriever,
            DocumentIndexer(retriever, assets=assets),
            RetrievalService(retriever, top_k),
            combined,
        )

    async def add(
        self,
        name: str,
        text: str,
        roles: tuple[str, ...],
        *,
        scope: str = "tenant",
        tenant_id: str | None = None,
        title: str | None = None,
        assets: tuple[Asset, ...] = (),
    ) -> None:
        document_id = str(uuid5(NAMESPACE_URL, name))
        self._names[document_id] = name
        await register_and_index(
            self.pool,
            self.indexer,
            DocumentForIndexing(
                document_id=document_id,
                scope=scope,  # type: ignore[arg-type]
                tenant_id=tenant_id if scope == "tenant" else None,
                title=title or name,
                text=text,
                allowed_roles=roles,
                assets=assets,
            ),
        )

    def names(self, chunks: Iterable[RetrievedChunk]) -> list[str]:
        return [self._names[chunk.document_id] for chunk in chunks]

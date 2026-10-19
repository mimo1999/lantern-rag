"""Ingest the synthetic demo corpus through the real stack (PostgreSQL/pgvector + Ollama).

Run after `python -m scripts.seed_db`:  uv run python -m scripts.ingest_documents
Idempotent: documents already indexed with the same content and roles are skipped.
Use --default-only to load just the shared (all-tenant) documents.
"""

import argparse
import asyncio

import asyncpg

from app.config import get_settings
from app.ingestion.indexer import DocumentIndexer
from app.ingestion.registry import PostgresDocumentRegistry
from app.ingestion.service import IngestionService, NewDocument
from app.retrieval.embeddings import OllamaEmbedder
from app.retrieval.pgvector import PgVectorRetriever
from evaluation.corpus import load_corpus


async def _main(default_only: bool) -> None:
    settings = get_settings()
    pool = await asyncpg.create_pool(min_size=1, max_size=2, **settings.postgres_connect_kwargs)
    retriever = PgVectorRetriever(
        pool,
        OllamaEmbedder(settings.ollama_url, settings.embedding_model, settings.embedding_dimension),
    )
    indexer = DocumentIndexer(retriever, settings.chunk_size_chars, settings.chunk_overlap_chars)
    service = IngestionService(PostgresDocumentRegistry(pool), indexer)
    try:
        for document in load_corpus():
            if default_only and document.scope != "default":
                continue
            result = await service.ingest(
                NewDocument(
                    scope=document.scope,
                    tenant_id=document.tenant_id,
                    title=document.title,
                    text=document.text,
                    allowed_roles=document.allowed_roles,
                    created_by="ingest-script",
                    source_uri=document.file,
                )
            )
            status = "already indexed" if result.duplicate else f"{result.chunks_indexed} chunks"
            print(f"{document.file:<32} {document.scope:<8} {status}")
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--default-only", action="store_true")
    asyncio.run(_main(parser.parse_args().default_only))

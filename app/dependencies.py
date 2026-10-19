from functools import lru_cache

import asyncpg
from fastapi import Depends

from app.config import Settings, get_settings
from app.db.dependencies import get_db_pool
from app.ingestion.indexer import DocumentIndexer
from app.retrieval.combined import CombinedRetriever
from app.retrieval.embeddings import OllamaEmbedder
from app.retrieval.pgvector import PgVectorRetriever
from app.retrieval.service import RetrievalService


@lru_cache(maxsize=4)
def _retriever_for(
    pool: asyncpg.Pool, ollama_url: str, model: str, dimension: int
) -> PgVectorRetriever:
    return PgVectorRetriever(pool, OllamaEmbedder(ollama_url, model, dimension))


@lru_cache(maxsize=4)
def _asset_retriever_for(
    pool: asyncpg.Pool, ollama_url: str, model: str, dimension: int
) -> PgVectorRetriever:
    return PgVectorRetriever(pool, OllamaEmbedder(ollama_url, model, dimension), "asset_chunks")


def get_asset_retriever(
    pool: asyncpg.Pool = Depends(get_db_pool), settings: Settings = Depends(get_settings)
) -> PgVectorRetriever:
    return _asset_retriever_for(
        pool,
        settings.ollama_url,
        settings.asset_embedding_model,
        settings.asset_embedding_dimension,
    )


def get_vector_retriever(
    pool: asyncpg.Pool = Depends(get_db_pool), settings: Settings = Depends(get_settings)
) -> PgVectorRetriever:
    return _retriever_for(
        pool, settings.ollama_url, settings.embedding_model, settings.embedding_dimension
    )


def get_retrieval_service(
    retriever: PgVectorRetriever = Depends(get_vector_retriever),
    assets: PgVectorRetriever = Depends(get_asset_retriever),
    settings: Settings = Depends(get_settings),
) -> RetrievalService:
    combined = CombinedRetriever(retriever, assets, settings.asset_top_k)
    return RetrievalService(combined, settings.top_k, settings.retrieval_relative_cutoff)


def get_document_indexer(
    retriever: PgVectorRetriever = Depends(get_vector_retriever),
    assets: PgVectorRetriever = Depends(get_asset_retriever),
    settings: Settings = Depends(get_settings),
) -> DocumentIndexer:
    return DocumentIndexer(
        retriever, settings.chunk_size_chars, settings.chunk_overlap_chars, assets
    )

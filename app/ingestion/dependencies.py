import asyncpg
from fastapi import Depends

from app.config import Settings, get_settings
from app.db.dependencies import get_db_pool
from app.dependencies import get_document_indexer
from app.ingestion.captioner import OllamaCaptioner
from app.ingestion.indexer import DocumentIndexer
from app.ingestion.registry import PostgresDocumentRegistry
from app.ingestion.service import IngestionService


def get_document_registry(pool: asyncpg.Pool = Depends(get_db_pool)) -> PostgresDocumentRegistry:
    return PostgresDocumentRegistry(pool)


def get_ingestion_service(
    pool: asyncpg.Pool = Depends(get_db_pool),
    indexer: DocumentIndexer = Depends(get_document_indexer),
    settings: Settings = Depends(get_settings),
) -> IngestionService:
    captioner = OllamaCaptioner(settings.ollama_url, settings.vision_model)
    return IngestionService(PostgresDocumentRegistry(pool), indexer, captioner)

import asyncpg
from fastapi import Depends

from app.audit.repository import AuditRepository, PostgresAuditRepository
from app.audit.service import AuditService
from app.config import Settings, get_settings
from app.db.dependencies import get_db_pool


def get_audit_repository(pool: asyncpg.Pool = Depends(get_db_pool)) -> AuditRepository:
    return PostgresAuditRepository(pool)


def get_audit_service(
    repository: AuditRepository = Depends(get_audit_repository),
    settings: Settings = Depends(get_settings),
) -> AuditService:
    return AuditService(repository, settings.embedding_model)

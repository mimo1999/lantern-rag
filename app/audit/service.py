import hashlib
import logging

from app.audit.models import AuditEntry
from app.audit.repository import AuditRepository
from app.auth.models import UserContext
from app.generation.service import AnswerResult

logger = logging.getLogger(__name__)


class AuditError(RuntimeError):
    """The audit record could not be written; the request must not return data."""


def hash_query(query: str) -> str:
    return hashlib.sha256(" ".join(query.split()).lower().encode()).hexdigest()


class AuditService:
    def __init__(self, repository: AuditRepository, embedding_model: str) -> None:
        self._repository = repository
        self._embedding_model = embedding_model

    async def record_answer(
        self, request_id: str, user: UserContext, query: str, result: AnswerResult
    ) -> None:
        """Fail closed: raises AuditError so an unaudited answer is never returned."""
        entry = AuditEntry(
            request_id=request_id,
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            roles=user.roles,
            query_hash=hash_query(query),
            retrieved_chunks=[
                {
                    "document_id": chunk.document_id,
                    "chunk_id": chunk.chunk_id,
                    "scope": chunk.scope,
                    "score": chunk.score,
                }
                for chunk in result.chunks_used
            ],
            outcome=result.outcome,
            llm_model=result.llm_model,
            embedding_model=self._embedding_model,
            retrieval_latency_ms=result.retrieval_ms,
            generation_latency_ms=result.generation_ms,
        )
        try:
            await self._repository.append(entry)
        except Exception as error:
            logger.exception("audit write failed", extra={"request_id": request_id})
            raise AuditError("audit write failed") from error

    async def record_error(self, request_id: str, user: UserContext, query: str) -> None:
        """Best effort: the request already failed, so a write failure is only logged."""
        entry = AuditEntry(
            request_id=request_id,
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            roles=user.roles,
            query_hash=hash_query(query),
            retrieved_chunks=[],
            outcome="error",
            embedding_model=self._embedding_model,
        )
        try:
            await self._repository.append(entry)
        except Exception:
            logger.exception("audit write failed for errored request")

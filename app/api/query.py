import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.audit.dependencies import get_audit_service
from app.audit.service import AuditError, AuditService
from app.auth.authentication import get_current_user
from app.auth.models import UserContext
from app.generation.dependencies import get_answer_service
from app.generation.provider import GenerationError
from app.generation.service import AnswerResult, AnswerService
from app.observability import metrics
from app.observability.context import get_request_id
from app.retrieval.embeddings import EmbeddingError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/query", tags=["query"])


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4_000)


class Source(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    document_id: str
    chunk_id: str
    title: str
    scope: str
    score: float
    kind: str = "text"


class QueryResponse(BaseModel):
    request_id: str
    answer: str
    sources: list[Source]


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="The answering service is temporarily unavailable",
    )


def _record_query_metrics(result: AnswerResult) -> None:
    metrics.RAG_QUERIES.labels(result.outcome).inc()
    metrics.RAG_RESULTS.observe(len(result.chunks_used))
    metrics.RETRIEVAL_LATENCY.observe(result.retrieval_ms / 1000)
    if result.generation_ms is not None:
        metrics.LLM_LATENCY.observe(result.generation_ms / 1000)
    if result.input_tokens:
        metrics.LLM_TOKENS.labels("input").inc(result.input_tokens)
    if result.output_tokens:
        metrics.LLM_TOKENS.labels("output").inc(result.output_tokens)


@router.post("", response_model=QueryResponse)
async def query_documents(
    request: QueryRequest,
    user: UserContext = Depends(get_current_user),
    answers: AnswerService = Depends(get_answer_service),
    audit: AuditService = Depends(get_audit_service),
    request_id: str = Depends(get_request_id),
) -> QueryResponse:
    try:
        result = await answers.answer(request.query, user)
    except (EmbeddingError, GenerationError) as error:
        kind = "embedding" if isinstance(error, EmbeddingError) else "llm"
        metrics.BACKEND_ERRORS.labels(kind).inc()
        await audit.record_error(request_id, user, request.query)
        raise _unavailable() from error
    try:
        await audit.record_answer(request_id, user, request.query, result)
    except AuditError as error:
        metrics.AUDIT_FAILURES.inc()
        raise _unavailable() from error
    _record_query_metrics(result)
    logger.info(
        "query_completed",
        extra={
            "user_id": user.user_id,
            "tenant_id": user.tenant_id,
            "outcome": result.outcome,
            "result_count": len(result.chunks_used),
            "retrieval_ms": round(result.retrieval_ms, 1),
            "generation_ms": (
                None if result.generation_ms is None else round(result.generation_ms, 1)
            ),
        },
    )
    return QueryResponse(
        request_id=request_id,
        answer=result.answer,
        sources=[Source.model_validate(chunk) for chunk in result.chunks_used],
    )

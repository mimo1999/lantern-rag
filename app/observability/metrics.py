"""Prometheus metrics. Labels are deliberately low-cardinality and never include tenant,
user, query, or document identifiers."""

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from app.config import Settings, get_settings

HTTP_REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["method", "route"]
)
AUTH_DENIED = Counter(
    "authorization_denied_total", "Requests rejected with 401 or 403", ["status"]
)
RAG_QUERIES = Counter("rag_queries_total", "Answered /query requests", ["outcome"])
RAG_RESULTS = Histogram(
    "retrieval_results_count",
    "Chunks placed in the LLM context per query",
    buckets=(0, 1, 2, 3, 5, 8, 13, 20),
)
RETRIEVAL_LATENCY = Histogram("retrieval_latency_seconds", "Embedding + vector search latency")
LLM_LATENCY = Histogram(
    "llm_latency_seconds",
    "LLM generation latency",
    buckets=(0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128),
)
LLM_TOKENS = Counter("llm_tokens_total", "LLM tokens used", ["direction"])
AUDIT_FAILURES = Counter("audit_write_failures_total", "Audit writes that failed")
INGESTION_JOBS = Counter(
    "ingestion_jobs_total",
    "Background upload jobs by outcome (ready, duplicate, failed)",
    ["outcome"],
)
BACKEND_ERRORS = Counter("backend_errors_total", "Embedding or LLM failures", ["kind"])

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    """Keep this endpoint on an internal network. If METRICS_TOKEN is set it is enforced."""
    expected = settings.metrics_token.get_secret_value() if settings.metrics_token else None
    if expected:
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid metrics token")
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

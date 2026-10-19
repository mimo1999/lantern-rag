from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class AuditEntry:
    request_id: str
    user_id: str
    tenant_id: str
    roles: tuple[str, ...]
    query_hash: str
    retrieved_chunks: list[dict[str, Any]]
    outcome: str
    authorization_decision: str = "allow"
    llm_model: str | None = None
    embedding_model: str | None = None
    retrieval_latency_ms: float | None = None
    generation_latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class AuditRecord:
    id: str
    occurred_at: datetime
    entry: AuditEntry


@dataclass(frozen=True, slots=True)
class AuditFilter:
    tenant_id: str | None = None
    user_id: str | None = None
    document_id: str | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    limit: int = 50
    offset: int = 0

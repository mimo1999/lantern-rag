from dataclasses import asdict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.audit.dependencies import get_audit_repository
from app.audit.models import AuditFilter
from app.audit.repository import AuditRepository
from app.auth.authentication import PLATFORM_ADMIN_ROLE, require_role
from app.auth.models import UserContext

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditItem(BaseModel):
    id: str
    request_id: str
    occurred_at: datetime
    user_id: str
    tenant_id: str
    roles: list[str]
    query_hash: str
    retrieved_chunks: list[dict[str, Any]]
    llm_model: str | None
    embedding_model: str | None
    retrieval_latency_ms: float | None
    generation_latency_ms: float | None
    authorization_decision: str
    outcome: str


@router.get("", response_model=list[AuditItem])
async def list_audit_records(
    user_id: str | None = None,
    document_id: str | None = None,
    tenant_id: str | None = None,
    occurred_from: datetime | None = Query(default=None, alias="from"),
    occurred_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: UserContext = Depends(require_role("admin")),
    repository: AuditRepository = Depends(get_audit_repository),
) -> list[AuditItem]:
    """Tenant admins only ever see their own tenant. `tenant_id` is honored solely for
    platform admins; for everyone else it is ignored and the caller's tenant is forced."""
    is_platform_admin = PLATFORM_ADMIN_ROLE in user.roles
    scoped_tenant = tenant_id if is_platform_admin else user.tenant_id
    records = await repository.search(
        AuditFilter(
            tenant_id=scoped_tenant,
            user_id=user_id,
            document_id=document_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            limit=limit,
            offset=offset,
        )
    )
    return [
        AuditItem(id=record.id, occurred_at=record.occurred_at, **asdict(record.entry))
        for record in records
    ]

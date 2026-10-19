import json
from typing import Protocol

import asyncpg

from app.audit.models import AuditEntry, AuditFilter, AuditRecord


class AuditRepository(Protocol):
    async def append(self, entry: AuditEntry) -> None: ...

    async def search(self, criteria: AuditFilter) -> list[AuditRecord]: ...


class PostgresAuditRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def append(self, entry: AuditEntry) -> None:
        await self._pool.execute(
            "INSERT INTO audit_log (request_id, user_id, tenant_id, roles, query_hash,"
            " retrieved_chunks, llm_model, embedding_model, retrieval_latency_ms,"
            " generation_latency_ms, authorization_decision, outcome)"
            " VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, $12)",
            entry.request_id,
            entry.user_id,
            entry.tenant_id,
            list(entry.roles),
            entry.query_hash,
            json.dumps(entry.retrieved_chunks),
            entry.llm_model,
            entry.embedding_model,
            entry.retrieval_latency_ms,
            entry.generation_latency_ms,
            entry.authorization_decision,
            entry.outcome,
        )

    async def search(self, criteria: AuditFilter) -> list[AuditRecord]:
        clauses: list[str] = []
        params: list[object] = []

        def add(template: str, value: object) -> None:
            params.append(value)
            clauses.append(template.replace("$N", f"${len(params)}"))

        if criteria.tenant_id is not None:
            add("tenant_id = $N", criteria.tenant_id)
        if criteria.user_id is not None:
            add("user_id = $N", criteria.user_id)
        if criteria.document_id is not None:
            document_match = json.dumps([{"document_id": criteria.document_id}])
            add("retrieved_chunks @> $N::jsonb", document_match)
        if criteria.occurred_from is not None:
            add("occurred_at >= $N", criteria.occurred_from)
        if criteria.occurred_to is not None:
            add("occurred_at <= $N", criteria.occurred_to)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params += [criteria.limit, criteria.offset]
        rows = await self._pool.fetch(
            "SELECT id::text, request_id::text, occurred_at, user_id, tenant_id, roles,"
            " query_hash, retrieved_chunks::text AS retrieved_chunks, llm_model,"
            " embedding_model, retrieval_latency_ms, generation_latency_ms,"
            " authorization_decision, outcome"
            f" FROM audit_log {where} ORDER BY occurred_at DESC, id"
            f" LIMIT ${len(params) - 1} OFFSET ${len(params)}",
            *params,
        )
        return [
            AuditRecord(
                id=row["id"],
                occurred_at=row["occurred_at"],
                entry=AuditEntry(
                    request_id=row["request_id"],
                    user_id=row["user_id"],
                    tenant_id=row["tenant_id"],
                    roles=tuple(row["roles"]),
                    query_hash=row["query_hash"],
                    retrieved_chunks=json.loads(row["retrieved_chunks"]),
                    outcome=row["outcome"],
                    authorization_decision=row["authorization_decision"],
                    llm_model=row["llm_model"],
                    embedding_model=row["embedding_model"],
                    retrieval_latency_ms=row["retrieval_latency_ms"],
                    generation_latency_ms=row["generation_latency_ms"],
                ),
            )
            for row in rows
        ]

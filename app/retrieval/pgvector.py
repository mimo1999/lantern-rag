import logging
from collections.abc import Sequence
from dataclasses import dataclass

import asyncpg

from app.retrieval.embeddings import Embedder
from app.retrieval.filters import AuthorizationFilter
from app.retrieval.service import DocumentSummary, RetrievedChunk

logger = logging.getLogger(__name__)

# pgvector 0.8 can keep scanning the HNSW index until enough rows pass the WHERE filter;
# without it a selective tenant/role filter could return fewer than `limit` rows.
_ITERATIVE_SCAN_MIN_VERSION = (0, 8, 0)


class VectorStoreConfigError(RuntimeError):
    """The database schema does not match the configured embedding setup."""


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    id: str
    document_id: str
    chunk_id: str
    scope: str
    tenant_id: str | None
    allowed_roles: tuple[str, ...]
    title: str
    source_uri: str | None
    content: str
    embedding: list[float]
    kind: str = "text"  # "text" in document_chunks; "table" or "image" in asset_chunks
    page: int | None = None


def vector_literal(values: Sequence[float]) -> str:
    """pgvector's text input format, e.g. '[0.1,0.2]'. Bound as a parameter, never inlined."""
    return "[" + ",".join(repr(float(value)) for value in values) + "]"


def _parse_version(raw: str) -> tuple[int, ...]:
    return tuple(int(part) for part in raw.split(".")[:3] if part.isdigit())


_TABLES = {"document_chunks", "asset_chunks"}  # fixed names: the only ones interpolated


class PgVectorRetriever:
    """Vector search over one chunk table (`document_chunks` for prose, `asset_chunks` for
    tables and images). The authorization filter is a required argument of every read, so no
    method can return chunks without a caller-derived filter."""

    def __init__(self, pool: asyncpg.Pool, embedder: Embedder, table: str = "document_chunks"):
        if table not in _TABLES:
            raise ValueError(f"unknown chunk table {table!r}")
        self._pool = pool
        self._embedder = embedder
        self._table = table
        self._verified = False
        self._iterative_scan = False

    async def ensure_ready(self) -> None:
        """Fail fast on a missing extension or a dimension mismatch, once per process."""
        if self._verified:
            return
        async with self._pool.acquire() as conn:
            version = await conn.fetchval(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )
            if version is None:
                raise VectorStoreConfigError("pgvector extension is not installed; run migrations")
            dimension = await conn.fetchval(
                "SELECT atttypmod FROM pg_attribute WHERE attrelid = $1::regclass"
                " AND attname = 'embedding'",
                self._table,
            )
        if dimension != self._embedder.dimension:
            raise VectorStoreConfigError(
                f"{self._table}.embedding is vector({dimension}) but the embedder produces "
                f"{self._embedder.dimension} dimensions; add a migration and re-embed"
            )
        self._iterative_scan = _parse_version(version) >= _ITERATIVE_SCAN_MIN_VERSION
        self._verified = True

    async def search(
        self, query: str, authorization_filter: AuthorizationFilter, limit: int
    ) -> list[RetrievedChunk]:
        await self.ensure_ready()
        embedding = vector_literal(await self._embedder.embed(query))
        where, params = authorization_filter.to_sql(first_param=3)
        sql = (
            "SELECT document_id::text AS document_id, chunk_id, title, scope, content, kind,"
            " 1 - (embedding <=> $1::vector) AS score"
            f" FROM {self._table} WHERE embedding_model = $2 AND {where}"
            " ORDER BY embedding <=> $1::vector LIMIT $" + str(3 + len(params))
        )
        async with self._pool.acquire() as conn, conn.transaction():
            if self._iterative_scan:
                await conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
            rows = await conn.fetch(sql, embedding, self._embedder.model, *params, limit)
        return [
            RetrievedChunk(
                document_id=row["document_id"],
                chunk_id=row["chunk_id"],
                title=row["title"],
                scope=row["scope"],
                text=row["content"],
                score=float(row["score"]),
                kind=row["kind"],
            )
            for row in rows
        ]

    async def list_documents(
        self, authorization_filter: AuthorizationFilter
    ) -> list[DocumentSummary]:
        """Distinct documents visible under the filter; same predicate as `search`."""
        await self.ensure_ready()
        where, params = authorization_filter.to_sql(first_param=1)
        rows = await self._pool.fetch(
            "SELECT DISTINCT ON (document_id) document_id::text AS document_id, title, scope,"
            f" tenant_id, source_uri, allowed_roles FROM {self._table}"
            f" WHERE {where} ORDER BY document_id, chunk_id",
            *params,
        )
        summaries = [
            DocumentSummary(
                document_id=row["document_id"],
                title=row["title"],
                scope=row["scope"],
                tenant_id=row["tenant_id"],
                source_uri=row["source_uri"],
                allowed_roles=tuple(row["allowed_roles"]),
            )
            for row in rows
        ]
        return sorted(summaries, key=lambda summary: summary.title)

    @property
    def pool(self) -> asyncpg.Pool:
        return self._pool

    async def upsert_chunks(
        self, chunks: Sequence[ChunkRecord], conn: asyncpg.Connection | None = None
    ) -> None:
        """Insert all of a document's chunks atomically: either every chunk lands or none.
        With `conn`, the caller owns the transaction (used to write two tables as one unit)."""
        if not chunks:
            return
        await self.ensure_ready()
        rows = [
            (
                chunk.id,
                chunk.document_id,
                chunk.chunk_id,
                chunk.scope,
                chunk.tenant_id,
                list(chunk.allowed_roles),
                chunk.title,
                chunk.source_uri,
                chunk.content,
                self._embedder.model,
                vector_literal(chunk.embedding),
                chunk.kind,
                chunk.page,
            )
            for chunk in chunks
        ]
        if conn is not None:
            await self._insert(conn, rows)
            return
        async with self._pool.acquire() as own, own.transaction():
            await self._insert(own, rows)

    async def _insert(self, conn: asyncpg.Connection, rows: list[tuple]) -> None:
        await conn.executemany(
            f"INSERT INTO {self._table} (id, document_id, chunk_id, scope, tenant_id,"
            " allowed_roles, title, source_uri, content, embedding_model, embedding,"
            " kind, page)"
            " VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10, $11::vector,"
            " $12, $13)"
            " ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content,"
            " embedding = EXCLUDED.embedding, embedding_model = EXCLUDED.embedding_model",
            rows,
        )

    @property
    def embedder(self) -> Embedder:
        return self._embedder

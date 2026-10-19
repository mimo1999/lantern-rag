from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import asyncpg


@dataclass(frozen=True, slots=True)
class RegisteredDocument:
    id: str
    scope: str
    tenant_id: str | None
    title: str
    source_uri: str | None
    checksum: str
    allowed_roles: tuple[str, ...]
    status: str
    chunk_count: int
    created_by: str


# An 'indexing' row older than this was abandoned (server restart, crash), so it is reported as
# failed instead of "processing" forever. Longer than any legitimate job (Docling's 5-minute
# budget plus captioning up to 25 images).
STALE_INDEXING_MINUTES = 30
STALE_ERROR = "Indexing was interrupted. Upload the file again."


@dataclass(frozen=True, slots=True)
class UploadStatus:
    document_id: str
    title: str
    source_uri: str | None
    status: str  # indexing | ready | failed | duplicate
    error: str | None
    chunks_indexed: int
    tables_indexed: int
    images_indexed: int
    images_skipped: int
    duplicate_of: str | None
    created_at: datetime


class DocumentRegistry(Protocol):
    async def create(self, document: RegisteredDocument) -> None: ...

    async def update_checksum(self, document_id: str, checksum: str) -> None: ...

    async def mark_ready(
        self,
        document_id: str,
        chunk_count: int,
        tables: int = 0,
        images: int = 0,
        images_skipped: int = 0,
    ) -> None: ...

    async def mark_failed(self, document_id: str, error: str | None = None) -> None:
        """Only an 'indexing' row can fail, so a late failure never overwrites a result."""
        ...

    async def mark_duplicate(self, document_id: str, duplicate_of: str) -> None: ...

    async def get_status(
        self, document_id: str, tenant_id: str, *, include_default: bool
    ) -> UploadStatus | None: ...

    async def list_unfinished(
        self, tenant_id: str, *, include_default: bool, limit: int = 20
    ) -> list[UploadStatus]: ...

    async def find_ready_duplicate(
        self, scope: str, tenant_id: str | None, checksum: str
    ) -> RegisteredDocument | None: ...

    async def delete(self, document_id: str, tenant_id: str, *, include_default: bool) -> bool: ...


class PostgresDocumentRegistry:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(self, document: RegisteredDocument) -> None:
        await self._pool.execute(
            "INSERT INTO documents (id, scope, tenant_id, title, source_uri, checksum,"
            " allowed_roles, status, chunk_count, created_by)"
            " VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
            document.id,
            document.scope,
            document.tenant_id,
            document.title,
            document.source_uri,
            document.checksum,
            list(document.allowed_roles),
            document.status,
            document.chunk_count,
            document.created_by,
        )

    async def update_checksum(self, document_id: str, checksum: str) -> None:
        await self._pool.execute(
            "UPDATE documents SET checksum = $2 WHERE id = $1::uuid", document_id, checksum
        )

    async def mark_ready(
        self,
        document_id: str,
        chunk_count: int,
        tables: int = 0,
        images: int = 0,
        images_skipped: int = 0,
    ) -> None:
        await self._pool.execute(
            "UPDATE documents SET status = 'ready', chunk_count = $2, tables_indexed = $3,"
            " images_indexed = $4, images_skipped = $5, error = NULL WHERE id = $1::uuid",
            document_id,
            chunk_count,
            tables,
            images,
            images_skipped,
        )

    async def mark_failed(self, document_id: str, error: str | None = None) -> None:
        await self._pool.execute(
            "UPDATE documents SET status = 'failed', error = $2"
            " WHERE id = $1::uuid AND status = 'indexing'",
            document_id,
            error,
        )

    async def mark_duplicate(self, document_id: str, duplicate_of: str) -> None:
        await self._pool.execute(
            "UPDATE documents SET status = 'duplicate', duplicate_of = $2::uuid"
            " WHERE id = $1::uuid AND status = 'indexing'",
            document_id,
            duplicate_of,
        )

    # A document belongs to the caller if it is their tenant's, or (platform-admin only) shared.
    _OWNED = "((scope = 'tenant' AND tenant_id = $1) OR (scope = 'default' AND $2))"
    _COLUMNS = (
        "id::text AS id, title, source_uri, error, chunk_count, tables_indexed,"
        " images_indexed, images_skipped, duplicate_of::text AS duplicate_of, created_at,"
        " CASE WHEN status = 'indexing' AND created_at < now() - make_interval(mins => $3)"
        " THEN 'failed' ELSE status END AS status,"
        " CASE WHEN status = 'indexing' AND created_at < now() - make_interval(mins => $3)"
        " THEN $4 ELSE error END AS shown_error"
    )

    async def get_status(
        self, document_id: str, tenant_id: str, *, include_default: bool
    ) -> UploadStatus | None:
        row = await self._pool.fetchrow(
            f"SELECT {self._COLUMNS} FROM documents WHERE id = $5::uuid AND {self._OWNED}",
            tenant_id,
            include_default,
            STALE_INDEXING_MINUTES,
            STALE_ERROR,
            document_id,
        )
        return None if row is None else _status(row)

    async def list_unfinished(
        self, tenant_id: str, *, include_default: bool, limit: int = 20
    ) -> list[UploadStatus]:
        """Uploads still processing, failed, or duplicates from the last day, newest first."""
        rows = await self._pool.fetch(
            f"SELECT {self._COLUMNS} FROM documents WHERE {self._OWNED} AND status <> 'ready'"
            " AND created_at > now() - interval '1 day' ORDER BY created_at DESC LIMIT $5",
            tenant_id,
            include_default,
            STALE_INDEXING_MINUTES,
            STALE_ERROR,
            limit,
        )
        return [_status(row) for row in rows]

    async def find_ready_duplicate(
        self, scope: str, tenant_id: str | None, checksum: str
    ) -> RegisteredDocument | None:
        row = await self._pool.fetchrow(
            "SELECT id::text, scope, tenant_id, title, source_uri, checksum, allowed_roles,"
            " status, chunk_count, created_by FROM documents"
            " WHERE scope = $1 AND tenant_id IS NOT DISTINCT FROM $2 AND checksum = $3"
            " AND status = 'ready' ORDER BY created_at LIMIT 1",
            scope,
            tenant_id,
            checksum,
        )
        if row is None:
            return None
        return RegisteredDocument(
            id=row["id"],
            scope=row["scope"],
            tenant_id=row["tenant_id"],
            title=row["title"],
            source_uri=row["source_uri"],
            checksum=row["checksum"],
            allowed_roles=tuple(row["allowed_roles"]),
            status=row["status"],
            chunk_count=row["chunk_count"],
            created_by=row["created_by"],
        )

    async def delete(self, document_id: str, tenant_id: str, *, include_default: bool) -> bool:
        """Delete one document and, via ON DELETE CASCADE, all of its vectors.

        Matches the document id AND the caller's tenant (default-scope documents only when
        `include_default`), so a tenant can never delete another tenant's or a shared document.
        """
        row = await self._pool.fetchval(
            "DELETE FROM documents WHERE id = $1::uuid AND"
            " ((scope = 'tenant' AND tenant_id = $2) OR (scope = 'default' AND $3))"
            " RETURNING id",
            document_id,
            tenant_id,
            include_default,
        )
        return row is not None


def _status(row: asyncpg.Record) -> UploadStatus:
    return UploadStatus(
        document_id=row["id"],
        title=row["title"],
        source_uri=row["source_uri"],
        status=row["status"],
        error=row["shown_error"],
        chunks_indexed=row["chunk_count"],
        tables_indexed=row["tables_indexed"],
        images_indexed=row["images_indexed"],
        images_skipped=row["images_skipped"],
        duplicate_of=row["duplicate_of"],
        created_at=row["created_at"],
    )

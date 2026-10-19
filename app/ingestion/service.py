import hashlib
import logging
from dataclasses import dataclass, replace
from uuid import uuid4

from app.ingestion.captioner import Captioner, CaptionError
from app.ingestion.indexer import (
    Asset,
    DocumentForIndexing,
    DocumentIndexer,
    DocumentScope,
)
from app.ingestion.loaders import ExtractedImage, ExtractedTable
from app.ingestion.registry import DocumentRegistry, RegisteredDocument
from app.retrieval.embeddings import EmbeddingError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NewDocument:
    scope: DocumentScope
    tenant_id: str | None
    title: str
    text: str
    allowed_roles: tuple[str, ...]
    created_by: str
    source_uri: str | None = None
    tables: tuple[ExtractedTable, ...] = ()
    images: tuple[ExtractedImage, ...] = ()
    images_dropped: int = 0  # figures the loader left out because of the per-document cap
    # Hash of the original file, so two PDFs with the same prose but different tables or
    # images are not mistaken for duplicates.
    fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class IngestionResult:
    document_id: str
    chunks_indexed: int
    duplicate: bool
    tables_indexed: int = 0
    images_indexed: int = 0
    images_skipped: int = 0


def content_checksum(text: str, allowed_roles: tuple[str, ...], fingerprint: str = "") -> str:
    """Identical text with a different ACL is a different document, so roles are hashed too."""
    normalized = " ".join(text.split())
    payload = normalized + "\x00" + fingerprint + "\x00" + ",".join(sorted(set(allowed_roles)))
    return hashlib.sha256(payload.encode()).hexdigest()


def _table_text(table: ExtractedTable) -> str:
    return f"Section: {table.heading}\n{table.markdown}" if table.heading else table.markdown


def _image_text(image: ExtractedImage, description: str | None) -> str | None:
    """What gets embedded for a figure: its section, the PDF's own caption (which usually holds
    the title the picture itself lacks) and the vision model's description. If the model failed
    but the PDF supplied a caption, the caption alone still makes the figure findable."""
    if description is None and not image.caption:
        return None
    lines = [f"Section: {image.heading}"] if image.heading else []
    if image.caption:
        lines.append(f"Figure caption: {image.caption}")
    if description:
        lines.append(f"Image description: {description}")
    return "\n".join(lines)


def _public_error(error: Exception) -> str:
    """What the uploader may see. Exception text can name internal hosts or queries."""
    if isinstance(error, ValueError):
        return str(error)  # our own validation messages
    if isinstance(error, EmbeddingError):
        return "The embedding service was unavailable. Upload the file again."
    return "Indexing failed. Upload the file again."


class IngestionService:
    """Registers a document, indexes its chunks, and records the outcome."""

    def __init__(
        self,
        registry: DocumentRegistry,
        indexer: DocumentIndexer,
        captioner: Captioner | None = None,
    ) -> None:
        self._registry = registry
        self._indexer = indexer
        self._captioner = captioner

    async def ingest(self, document: NewDocument) -> IngestionResult:
        indexable = DocumentForIndexing(
            document_id=str(uuid4()),
            scope=document.scope,
            tenant_id=document.tenant_id,
            title=document.title,
            text=document.text,
            allowed_roles=document.allowed_roles,
            source_uri=document.source_uri,
        )  # validates the scope/tenant pairing before anything is written
        checksum = content_checksum(document.text, document.allowed_roles, document.fingerprint)
        existing = await self._registry.find_ready_duplicate(
            document.scope, document.tenant_id, checksum
        )
        if existing is not None:
            return IngestionResult(existing.id, existing.chunk_count, duplicate=True)

        await self._registry.create(
            RegisteredDocument(
                id=indexable.document_id,
                scope=document.scope,
                tenant_id=document.tenant_id,
                title=document.title,
                source_uri=document.source_uri,
                checksum=checksum,
                allowed_roles=document.allowed_roles,
                status="indexing",
                chunk_count=0,
                created_by=document.created_by,
            )
        )
        return await self._index(indexable, document)

    async def start(
        self,
        *,
        scope: DocumentScope,
        tenant_id: str | None,
        title: str,
        source_uri: str | None,
        allowed_roles: tuple[str, ...],
        created_by: str,
    ) -> str:
        """Register an upload as 'indexing' before its file is parsed, and return its id."""
        indexable = DocumentForIndexing(
            document_id=str(uuid4()),
            scope=scope,
            tenant_id=tenant_id,
            title=title,
            text="",
            allowed_roles=allowed_roles,
            source_uri=source_uri,
        )  # validates the scope/tenant pairing before anything is written
        await self._registry.create(
            RegisteredDocument(
                id=indexable.document_id,
                scope=scope,
                tenant_id=tenant_id,
                title=title,
                source_uri=source_uri,
                checksum=f"pending:{indexable.document_id}",  # replaced once the text is known
                allowed_roles=allowed_roles,
                status="indexing",
                chunk_count=0,
                created_by=created_by,
            )
        )
        return indexable.document_id

    async def complete(self, document_id: str, document: NewDocument) -> IngestionResult:
        """Index a registered upload once its file has been parsed (or note that identical
        content was already indexed). Failures are recorded on the row and re-raised."""
        checksum = content_checksum(document.text, document.allowed_roles, document.fingerprint)
        existing = await self._registry.find_ready_duplicate(
            document.scope, document.tenant_id, checksum
        )
        if existing is not None:
            await self._registry.mark_duplicate(document_id, existing.id)
            return IngestionResult(existing.id, existing.chunk_count, duplicate=True)
        await self._registry.update_checksum(document_id, checksum)
        indexable = DocumentForIndexing(
            document_id=document_id,
            scope=document.scope,
            tenant_id=document.tenant_id,
            title=document.title,
            text=document.text,
            allowed_roles=document.allowed_roles,
            source_uri=document.source_uri,
        )
        return await self._index(indexable, document)

    async def fail(self, document_id: str, message: str) -> None:
        await self._registry.mark_failed(document_id, message)

    async def _index(
        self, indexable: DocumentForIndexing, document: NewDocument
    ) -> IngestionResult:
        skipped = document.images_dropped
        try:
            assets = [Asset("table", _table_text(t), t.page) for t in document.tables]
            for image in document.images:
                text = _image_text(image, await self._caption(image))
                if text is None:
                    skipped += 1
                else:
                    assets.append(Asset("image", text, image.page))
            result = await self._indexer.index(replace(indexable, assets=tuple(assets)))
        except Exception as error:
            await self._registry.mark_failed(indexable.document_id, _public_error(error))
            raise
        try:
            await self._registry.mark_ready(
                indexable.document_id, result.total, result.tables, result.images, skipped
            )
        except Exception:
            logger.exception("document indexed but status update failed")
        return IngestionResult(
            indexable.document_id,
            result.total,
            duplicate=False,
            tables_indexed=result.tables,
            images_indexed=result.images,
            images_skipped=skipped,
        )

    async def _caption(self, image: ExtractedImage) -> str | None:
        """An image the vision model cannot describe is skipped, not fatal: prose and tables
        stay searchable, and the skip is reported in the result and the log."""
        if self._captioner is None:
            return None
        try:
            return await self._captioner.caption(image.png)
        except CaptionError:
            logger.warning("image_caption_failed", extra={"page": image.page})
            return None

import hashlib
import logging
import re
from pathlib import PurePath
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field

from app.auth.authentication import PLATFORM_ADMIN_ROLE, get_current_user, require_role
from app.auth.models import UserContext
from app.config import Settings, get_settings
from app.dependencies import get_retrieval_service
from app.ingestion.dependencies import get_document_registry, get_ingestion_service
from app.ingestion.indexer import DocumentScope
from app.ingestion.loaders import SUPPORTED_EXTENSIONS
from app.ingestion.registry import DocumentRegistry, UploadStatus
from app.ingestion.service import IngestionService, NewDocument
from app.ingestion.uploads import UploadTracker, get_upload_tracker, process_upload
from app.retrieval.embeddings import EmbeddingError
from app.retrieval.service import DocumentSummary, RetrievalService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])

_ROLE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MULTIPART_OVERHEAD_BYTES = 64 * 1024


class DocumentRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=1_000_000)
    allowed_roles: list[str] = Field(min_length=1, max_length=20)
    source_uri: str | None = Field(default=None, max_length=2_000)


class DocumentResponse(BaseModel):
    document_id: str
    chunks_indexed: int  # prose passages plus tables plus images
    duplicate: bool = False
    tables_indexed: int = 0
    images_indexed: int = 0
    images_skipped: int = 0  # over the per-document cap, or the vision model failed


class UploadAccepted(BaseModel):
    """The file is registered and is being indexed in the background; poll the status."""

    document_id: str
    status: str = "processing"


class UploadStatusResponse(BaseModel):
    document_id: str
    title: str
    source_uri: str | None
    status: str  # processing | ready | failed | duplicate
    error: str | None
    chunks_indexed: int
    tables_indexed: int
    images_indexed: int
    images_skipped: int
    duplicate_of: str | None


def _status_response(row: UploadStatus) -> UploadStatusResponse:
    return UploadStatusResponse(
        document_id=row.document_id,
        title=row.title,
        source_uri=row.source_uri,
        status="processing" if row.status == "indexing" else row.status,
        error=row.error,
        chunks_indexed=row.chunks_indexed,
        tables_indexed=row.tables_indexed,
        images_indexed=row.images_indexed,
        images_skipped=row.images_skipped,
        duplicate_of=row.duplicate_of,
    )


async def _ingest(
    ingestion: IngestionService,
    user: UserContext,
    *,
    scope: DocumentScope,
    title: str,
    text: str,
    allowed_roles: list[str],
    source_uri: str | None,
) -> DocumentResponse:
    try:
        result = await ingestion.ingest(
            NewDocument(
                scope=scope,
                tenant_id=user.tenant_id if scope == "tenant" else None,
                title=title,
                text=text,
                allowed_roles=tuple(allowed_roles),
                created_by=user.user_id,
                source_uri=source_uri,
            )
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
        ) from error
    except EmbeddingError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The embedding service is temporarily unavailable",
        ) from error
    return DocumentResponse(
        document_id=result.document_id,
        chunks_indexed=result.chunks_indexed,
        duplicate=result.duplicate,
        tables_indexed=result.tables_indexed,
        images_indexed=result.images_indexed,
        images_skipped=result.images_skipped,
    )


def _parse_roles(raw: str) -> list[str]:
    roles = [role.strip() for role in raw.split(",") if role.strip()]
    if not roles or len(roles) > 20 or not all(_ROLE_PATTERN.match(role) for role in roles):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="allowed_roles must be 1-20 comma-separated role names (letters, digits, _ -)",
        )
    return roles


async def _accept_upload(
    ingestion: IngestionService,
    tracker: UploadTracker,
    background: BackgroundTasks,
    user: UserContext,
    settings: Settings,
    request: Request,
    *,
    scope: DocumentScope,
    file: UploadFile,
    allowed_roles: str,
    title: str | None,
) -> UploadAccepted:
    """Validate the upload cheaply, register it, and hand the slow work to a background task.
    Tenant, scope and roles are captured here from the verified token, never re-read later."""
    roles = _parse_roles(allowed_roles)
    limit = settings.max_upload_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + _MULTIPART_OVERHEAD_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="File is too large")
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="File is too large")

    filename = PurePath((file.filename or "upload").replace("\\", "/")).name[:200] or "upload"
    if PurePath(filename).suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Only .md, .txt and .pdf documents are supported",
        )
    if not tracker.try_acquire(user.tenant_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Several uploads are still being processed. Wait for one to finish.",
        )
    tenant_id = user.tenant_id if scope == "tenant" else None
    document_title = (title or PurePath(filename).stem)[:500]
    try:
        document_id = await ingestion.start(
            scope=scope,
            tenant_id=tenant_id,
            title=document_title,
            source_uri=filename,
            allowed_roles=tuple(roles),
            created_by=user.user_id,
        )
    except BaseException:
        tracker.release(user.tenant_id)
        raise
    background.add_task(
        process_upload,
        ingestion,
        tracker,
        user.tenant_id,
        document_id,
        filename,
        data,
        NewDocument(
            scope=scope,
            tenant_id=tenant_id,
            title=document_title,
            text="",
            allowed_roles=tuple(roles),
            created_by=user.user_id,
            source_uri=filename,
            # Same prose with different tables or images is a different PDF.
            fingerprint=hashlib.sha256(data).hexdigest()
            if filename.lower().endswith(".pdf")
            else "",
        ),
    )
    return UploadAccepted(document_id=document_id)


@router.get("", response_model=list[DocumentSummary])
async def list_documents(
    user: UserContext = Depends(get_current_user),
    retrieval: RetrievalService = Depends(get_retrieval_service),
) -> list[DocumentSummary]:
    """List documents visible to the caller: default documents plus their own tenant's."""
    return await retrieval.list_documents(user)


@router.get("/uploads", response_model=list[UploadStatusResponse])
async def list_uploads(
    user: UserContext = Depends(require_role("admin")),
    registry: DocumentRegistry = Depends(get_document_registry),
) -> list[UploadStatusResponse]:
    """The caller's recent uploads that are processing, failed or duplicates (last 24 hours).
    Finished documents are in `GET /documents`."""
    rows = await registry.list_unfinished(
        user.tenant_id, include_default=PLATFORM_ADMIN_ROLE in user.roles
    )
    return [_status_response(row) for row in rows]


@router.get("/{document_id}/status", response_model=UploadStatusResponse)
async def upload_status(
    document_id: UUID,
    user: UserContext = Depends(require_role("admin")),
    registry: DocumentRegistry = Depends(get_document_registry),
) -> UploadStatusResponse:
    """Progress and outcome of one upload. Another tenant's document is a 404, like delete."""
    row = await registry.get_status(
        str(document_id), user.tenant_id, include_default=PLATFORM_ADMIN_ROLE in user.roles
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found")
    return _status_response(row)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: UUID,
    user: UserContext = Depends(require_role("admin")),
    registry: DocumentRegistry = Depends(get_document_registry),
) -> Response:
    """Delete a document and its vectors. Tenant admins can delete their own tenant's
    documents; shared (default) documents need platform-admin. Anything else is a 404, so
    the endpoint cannot be used to probe for other tenants' document ids."""
    deleted = await registry.delete(
        str(document_id),
        user.tenant_id,
        include_default=PLATFORM_ADMIN_ROLE in user.roles,
    )
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Document not found")
    logger.info(
        "document_deleted",
        extra={
            "document_id": str(document_id),
            "tenant_id": user.tenant_id,
            "user_id": user.user_id,
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def index_tenant_document(
    request: DocumentRequest,
    user: UserContext = Depends(require_role("admin")),
    ingestion: IngestionService = Depends(get_ingestion_service),
) -> DocumentResponse:
    """Ingest a document scoped to the caller's own tenant.

    `tenant_id` is always the verified caller's tenant, never client-supplied, so a
    tenant admin can never write a chunk that another tenant could retrieve.
    """
    return await _ingest(
        ingestion,
        user,
        scope="tenant",
        title=request.title,
        text=request.content,
        allowed_roles=request.allowed_roles,
        source_uri=request.source_uri,
    )


@router.post("/default", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def index_default_document(
    request: DocumentRequest,
    user: UserContext = Depends(require_role(PLATFORM_ADMIN_ROLE)),
    ingestion: IngestionService = Depends(get_ingestion_service),
) -> DocumentResponse:
    """Ingest a document visible to every tenant. Requires the platform-admin role,
    which is distinct from any tenant's own `admin` role."""
    return await _ingest(
        ingestion,
        user,
        scope="default",
        title=request.title,
        text=request.content,
        allowed_roles=request.allowed_roles,
        source_uri=request.source_uri,
    )


@router.post("/upload", response_model=UploadAccepted, status_code=status.HTTP_202_ACCEPTED)
async def upload_tenant_document(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    allowed_roles: str = Form(...),
    title: str | None = Form(default=None),
    user: UserContext = Depends(require_role("admin")),
    ingestion: IngestionService = Depends(get_ingestion_service),
    tracker: UploadTracker = Depends(get_upload_tracker),
    settings: Settings = Depends(get_settings),
) -> UploadAccepted:
    """Upload a .md/.txt/.pdf file as a document scoped to the caller's own tenant. Returns at
    once; indexing runs in the background (see `GET /documents/{id}/status`)."""
    return await _accept_upload(
        ingestion,
        tracker,
        background,
        user,
        settings,
        request,
        scope="tenant",
        file=file,
        allowed_roles=allowed_roles,
        title=title,
    )


@router.post("/default/upload", response_model=UploadAccepted, status_code=status.HTTP_202_ACCEPTED)
async def upload_default_document(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    allowed_roles: str = Form(...),
    title: str | None = Form(default=None),
    user: UserContext = Depends(require_role(PLATFORM_ADMIN_ROLE)),
    ingestion: IngestionService = Depends(get_ingestion_service),
    tracker: UploadTracker = Depends(get_upload_tracker),
    settings: Settings = Depends(get_settings),
) -> UploadAccepted:
    """Upload a .md/.txt/.pdf file as a default document visible to every tenant (background
    indexing, like the tenant upload)."""
    return await _accept_upload(
        ingestion,
        tracker,
        background,
        user,
        settings,
        request,
        scope="default",
        file=file,
        allowed_roles=allowed_roles,
        title=title,
    )

"""Background processing of uploaded files.

The upload request only validates and registers the file; parsing (Docling, minutes for a
figure-heavy PDF), captioning and indexing happen after the response, and the outcome is
written to the document's registry row for the client to poll.

ponytail: jobs run inside the API process (Starlette background tasks), so a restart abandons
them (the stale-row rule in the registry reports those as failed) and each worker keeps its own
in-flight counts. Move to a real queue (a Postgres `FOR UPDATE SKIP LOCKED` table or Redis) when
the API runs several workers or uploads must survive restarts.
"""

import logging
import threading
from functools import lru_cache

from fastapi.concurrency import run_in_threadpool

from app.ingestion.loaders import extract_document
from app.ingestion.service import IngestionService, NewDocument
from app.observability import metrics

logger = logging.getLogger(__name__)

# Bounds the files held in memory (up to MAX_UPLOAD_BYTES each) and the work one tenant can queue.
MAX_UPLOADS_IN_FLIGHT_PER_TENANT = 3


class UploadTracker:
    """How many uploads each tenant has in progress in this process."""

    def __init__(self, limit: int = MAX_UPLOADS_IN_FLIGHT_PER_TENANT) -> None:
        self._limit = limit
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def try_acquire(self, tenant_id: str) -> bool:
        with self._lock:
            if self._counts.get(tenant_id, 0) >= self._limit:
                return False
            self._counts[tenant_id] = self._counts.get(tenant_id, 0) + 1
            return True

    def release(self, tenant_id: str) -> None:
        with self._lock:
            remaining = self._counts.get(tenant_id, 0) - 1
            if remaining > 0:
                self._counts[tenant_id] = remaining
            else:
                self._counts.pop(tenant_id, None)


@lru_cache
def get_upload_tracker() -> UploadTracker:
    return UploadTracker()


async def process_upload(
    ingestion: IngestionService,
    tracker: UploadTracker,
    owner: str,
    document_id: str,
    filename: str,
    data: bytes,
    document: NewDocument,
) -> None:
    """Parse, caption, embed and index one registered upload. Never raises: every outcome,
    including failure, ends up on the registry row. `document` carries the scope, tenant and
    roles captured from the verified token when the upload was accepted; `owner` is the tenant
    whose in-flight count to release."""
    try:
        extracted = await run_in_threadpool(extract_document, filename, data)
        result = await ingestion.complete(
            document_id,
            NewDocument(
                scope=document.scope,
                tenant_id=document.tenant_id,
                title=document.title,
                text=extracted.text,
                allowed_roles=document.allowed_roles,
                created_by=document.created_by,
                source_uri=document.source_uri,
                tables=extracted.tables,
                images=extracted.images,
                images_dropped=extracted.images_dropped,
                fingerprint=document.fingerprint,
            ),
        )
        outcome = "duplicate" if result.duplicate else "ready"
    except ValueError as error:  # unusable file; the message is ours and safe to show
        await ingestion.fail(document_id, str(error))
        outcome = "failed"
    except Exception:  # IngestionService has already recorded its own failures
        logger.exception("upload_processing_failed", extra={"document_id": document_id})
        await ingestion.fail(document_id, "Indexing failed. Upload the file again.")
        outcome = "failed"
    finally:
        tracker.release(owner)
    metrics.INGESTION_JOBS.labels(outcome).inc()
    logger.info(
        "upload_processed",
        extra={"document_id": document_id, "tenant_id": document.tenant_id, "outcome": outcome},
    )

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.ingestion.dependencies import get_ingestion_service
from app.ingestion.service import IngestionResult, NewDocument
from app.main import app
from app.retrieval.embeddings import EmbeddingError

SETTINGS = Settings(
    jwt_secret_key="test-secret-that-is-at-least-thirty-two-bytes", max_upload_bytes=1_000
)


class RecordingIngestion:
    """Stands in for IngestionService: records what was registered and what the background
    job later completed or failed."""

    def __init__(self) -> None:
        self.started: list[dict] = []
        self.documents: list[NewDocument] = []
        self.failures: list[str] = []
        self.complete_error: Exception | None = None

    async def start(self, **fields) -> str:
        self.started.append(fields)
        return "doc-id"

    async def complete(self, document_id: str, document: NewDocument) -> IngestionResult:
        if self.complete_error:
            raise self.complete_error
        self.documents.append(document)
        return IngestionResult(document_id, 2, duplicate=False)

    async def fail(self, document_id: str, message: str) -> None:
        self.failures.append(message)

    async def ingest(self, document: NewDocument) -> IngestionResult:  # the JSON endpoints
        self.documents.append(document)
        return IngestionResult("doc-id", 2, duplicate=False)


@pytest.fixture
def setup():
    ingestion = RecordingIngestion()
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    app.dependency_overrides[get_ingestion_service] = lambda: ingestion
    yield ingestion, TestClient(app)
    app.dependency_overrides.clear()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_tenant_upload_uses_verified_tenant_filename_title_and_parsed_roles(
    setup, token_for
) -> None:
    ingestion, client = setup

    response = client.post(
        "/documents/upload",
        files={"file": ("../../etc/Finance Plan.md", b"# Plan\nrevenue", "text/markdown")},
        data={"allowed_roles": "admin, finance", "tenant_id": "globex"},
        headers=auth(token_for("alice", "acme", ("admin",))),
    )

    assert response.status_code == 202
    assert response.json() == {"document_id": "doc-id", "status": "processing"}
    [started] = ingestion.started
    assert started["tenant_id"] == "acme" and started["scope"] == "tenant"
    [document] = ingestion.documents  # the background job ran after the response
    assert document.scope == "tenant" and document.tenant_id == "acme"
    assert document.allowed_roles == ("admin", "finance")
    assert document.title == "Finance Plan" and document.source_uri == "Finance Plan.md"
    assert document.created_by == "alice"


TOO_MANY_ROLES = ",".join(f"r{i}" for i in range(21))


@pytest.mark.parametrize("roles", ["", " , ", "admin,bad role!", TOO_MANY_ROLES])
def test_upload_rejects_invalid_roles(setup, token_for, roles) -> None:
    ingestion, client = setup

    response = client.post(
        "/documents/upload",
        files={"file": ("a.txt", b"text", "text/plain")},
        data={"allowed_roles": roles},
        headers=auth(token_for("alice", "acme", ("admin",))),
    )

    assert response.status_code == 422 and ingestion.documents == []


def test_upload_rejects_unsupported_and_oversized_files(setup, token_for) -> None:
    ingestion, client = setup
    headers = auth(token_for("alice", "acme", ("admin",)))

    exe = client.post(
        "/documents/upload",
        files={"file": ("a.exe", b"MZ", "application/octet-stream")},
        data={"allowed_roles": "admin"},
        headers=headers,
    )
    big = client.post(
        "/documents/upload",
        files={"file": ("a.txt", b"x" * 2_000, "text/plain")},
        data={"allowed_roles": "admin"},
        headers=headers,
    )

    assert exe.status_code == 422 and big.status_code == 413
    assert ingestion.documents == []


def test_employee_cannot_upload(setup, token_for) -> None:
    ingestion, client = setup

    response = client.post(
        "/documents/upload",
        files={"file": ("a.txt", b"text", "text/plain")},
        data={"allowed_roles": "employee"},
        headers=auth(token_for("bob", "acme", ("employee",))),
    )

    assert response.status_code == 403 and ingestion.documents == []


def test_default_upload_requires_platform_admin_and_has_no_tenant(setup, token_for) -> None:
    ingestion, client = setup
    files = {"file": ("handbook.txt", b"welcome", "text/plain")}

    denied = client.post(
        "/documents/default/upload",
        files=files,
        data={"allowed_roles": "employee"},
        headers=auth(token_for("alice", "acme", ("admin",))),
    )
    allowed = client.post(
        "/documents/default/upload",
        files=files,
        data={"allowed_roles": "employee"},
        headers=auth(token_for("root", "platform", ("platform-admin",))),
    )

    assert denied.status_code == 403 and allowed.status_code == 202
    [document] = ingestion.documents
    assert document.scope == "default" and document.tenant_id is None


def test_duplicate_flag_is_reported(token_for) -> None:
    class Duplicate:
        async def ingest(self, document: NewDocument) -> IngestionResult:
            return IngestionResult("existing", 5, duplicate=True)

    app.dependency_overrides[get_settings] = lambda: SETTINGS
    app.dependency_overrides[get_ingestion_service] = lambda: Duplicate()
    try:
        response = TestClient(app).post(
            "/documents",
            json={"title": "T", "content": "c", "allowed_roles": ["admin"]},
            headers=auth(token_for("alice", "acme", ("admin",))),
        )
    finally:
        app.dependency_overrides.clear()

    assert response.json() == {
        "document_id": "existing",
        "chunks_indexed": 5,
        "duplicate": True,
        "tables_indexed": 0,
        "images_indexed": 0,
        "images_skipped": 0,
    }


def test_embedding_outage_on_the_json_endpoint_is_a_503_not_a_500(token_for) -> None:
    class Down:
        async def ingest(self, document: NewDocument) -> IngestionResult:
            raise EmbeddingError("ollama down")

    app.dependency_overrides[get_settings] = lambda: SETTINGS
    app.dependency_overrides[get_ingestion_service] = lambda: Down()
    with TestClient(app) as client:
        response = client.post(
            "/documents",
            json={"title": "T", "content": "hello", "allowed_roles": ["admin"]},
            headers=auth(token_for("alice", "acme", ("admin",))),
        )
    app.dependency_overrides.clear()
    assert response.status_code == 503


def test_a_job_that_fails_records_the_failure_and_frees_the_slot(setup, token_for) -> None:
    ingestion, client = setup
    ingestion.complete_error = RuntimeError("boom")
    headers = auth(token_for("alice", "acme", ("admin",)))

    responses = [
        client.post(
            "/documents/upload",
            files={"file": ("a.txt", b"text", "text/plain")},
            data={"allowed_roles": "admin"},
            headers=headers,
        )
        for _ in range(5)  # more than the per-tenant limit, one after another
    ]

    assert [r.status_code for r in responses] == [202] * 5  # each failed job released its slot
    assert ingestion.failures == ["Indexing failed. Upload the file again."] * 5


def test_unreadable_files_fail_in_the_background_with_a_readable_message(setup, token_for) -> None:
    ingestion, client = setup

    response = client.post(
        "/documents/upload",
        files={"file": ("a.txt", b"\xff\xfe not utf-8", "text/plain")},
        data={"allowed_roles": "admin"},
        headers=auth(token_for("alice", "acme", ("admin",))),
    )

    assert response.status_code == 202
    assert ingestion.failures == ["Text documents must be UTF-8 encoded"]
    assert ingestion.documents == []


def test_too_many_uploads_in_flight_is_a_429(setup, token_for) -> None:
    from app.ingestion.uploads import MAX_UPLOADS_IN_FLIGHT_PER_TENANT, get_upload_tracker

    _, client = setup
    for _ in range(MAX_UPLOADS_IN_FLIGHT_PER_TENANT):
        assert get_upload_tracker().try_acquire("acme")

    blocked = client.post(
        "/documents/upload",
        files={"file": ("a.txt", b"text", "text/plain")},
        data={"allowed_roles": "admin"},
        headers=auth(token_for("alice", "acme", ("admin",))),
    )
    other_tenant = client.post(
        "/documents/upload",
        files={"file": ("a.txt", b"text", "text/plain")},
        data={"allowed_roles": "admin"},
        headers=auth(token_for("erin", "globex", ("admin",))),
    )

    assert blocked.status_code == 429 and other_tenant.status_code == 202

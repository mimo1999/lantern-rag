from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.audit.dependencies import get_audit_service
from app.auth.models import UserContext
from app.config import Settings, get_settings
from app.generation.dependencies import get_answer_service
from app.generation.provider import ChatMessage, Generation, GenerationError
from app.generation.service import AnswerService
from app.main import app
from app.retrieval.embeddings import EmbeddingError
from evaluation.testing import VectorEnv

GLOBEX_SECRET = "Globex confidential revenue is 112.7 million euros"
ACME_ADMIN_SECRET = "Acme executive compensation is 9 million euros"


class NullAudit:
    async def record_error(self, *args: object) -> None: ...


class RecordingProvider:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, messages: list[ChatMessage]) -> Generation:
        self.prompts.append("\n".join(message.content for message in messages))
        return Generation(text="answer", model="fake-llm", input_tokens=1, output_tokens=1)


@pytest.mark.asyncio
async def test_unauthorized_chunks_never_appear_in_llm_prompt(vector_env: VectorEnv) -> None:
    provider = RecordingProvider()
    service = AnswerService(vector_env.service, provider, 10_000)
    await vector_env.add(
        "g", GLOBEX_SECRET, ("employee", "admin"), tenant_id="globex", title="Globex Finance"
    )
    await vector_env.add(
        "a-exec", ACME_ADMIN_SECRET, ("admin",), tenant_id="acme", title="Acme Exec"
    )
    await vector_env.add(
        "a-public",
        "Acme employees receive 25 vacation days and confidential revenue is private.",
        ("employee", "admin"),
        tenant_id="acme",
        title="Acme Handbook",
    )

    bob = UserContext(user_id="bob", tenant_id="acme", roles=("employee",))
    result = await service.answer("What is the confidential revenue and compensation?", bob)

    assert result.outcome == "answered"
    assert vector_env.names(result.chunks_used) == ["a-public"]
    assert len(provider.prompts) == 1
    assert GLOBEX_SECRET not in provider.prompts[0]
    assert ACME_ADMIN_SECRET not in provider.prompts[0]


@pytest.mark.asyncio
async def test_llm_is_not_called_when_nothing_is_accessible(vector_env: VectorEnv) -> None:
    provider = RecordingProvider()
    service = AnswerService(vector_env.service, provider, 10_000)

    result = await service.answer("anything", UserContext("bob", "acme", ("employee",)))

    assert result.outcome == "no_results" and result.chunks_used == []
    assert provider.prompts == []


@pytest.mark.parametrize("error", [GenerationError("boom-detail"), EmbeddingError("down-detail")])
def test_api_maps_backend_failures_to_503_without_leaking_details(error) -> None:
    settings = Settings(jwt_secret_key="test-secret-that-is-at-least-thirty-two-bytes")

    class Failing:
        async def answer(self, question: str, user: UserContext):
            raise error

    token = jwt.encode(
        {
            "sub": "bob",
            "tenant_id": "acme",
            "roles": ["employee"],
            "iss": settings.jwt_issuer,
            "jti": "test-jti",
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        settings.jwt_secret_key,
        algorithm="HS256",
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_answer_service] = lambda: Failing()
    app.dependency_overrides[get_audit_service] = lambda: NullAudit()
    response = TestClient(app).post(
        "/query", json={"query": "q"}, headers={"Authorization": f"Bearer {token}"}
    )
    app.dependency_overrides.clear()

    assert response.status_code == 503
    assert str(error) not in response.text

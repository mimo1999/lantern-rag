import base64
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient

from app.audit.dependencies import get_audit_service
from app.auth.models import UserContext
from app.config import Settings, get_settings
from app.generation.dependencies import get_answer_service
from app.generation.service import AnswerResult
from app.main import app

SETTINGS = Settings(jwt_secret_key="test-secret-that-is-at-least-thirty-two-bytes")
ENDPOINTS = [
    ("POST", "/query", {"query": "q"}),
    ("GET", "/documents", None),
    ("GET", "/audit", None),
]


def claims(**overrides: object) -> dict[str, object]:
    return {
        "sub": "bob",
        "tenant_id": "acme",
        "roles": ["employee"],
        "iss": SETTINGS.jwt_issuer,
        "jti": "test-jti",
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        **overrides,
    }


def sign(payload: dict[str, object], key: str = SETTINGS.jwt_secret_key) -> str:
    return jwt.encode(payload, key, algorithm="HS256")


def unsigned_token(payload: dict[str, object]) -> str:
    def encode(part: dict[str, object]) -> str:
        raw = json.dumps(part, default=lambda v: int(v.timestamp())).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(payload)}."


BAD_TOKENS = {
    "forged signature": sign(claims(roles=["admin", "platform-admin"]), key="x" * 40),
    "alg none": unsigned_token(claims(roles=["admin"])),
    "expired": sign(claims(exp=datetime.now(UTC) - timedelta(minutes=1))),
    "wrong issuer": sign(claims(iss="someone-else")),
    "missing tenant": sign({k: v for k, v in claims().items() if k != "tenant_id"}),
    "missing exp": sign({k: v for k, v in claims().items() if k != "exp"}),
    "roles as string": sign(claims(roles="admin")),
    "roles with non-string": sign(claims(roles=["admin", 7])),
    "garbage": "not.a.jwt",
}


@pytest.fixture
def client():
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.parametrize("name", BAD_TOKENS)
@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
def test_every_protected_endpoint_rejects_bad_tokens(client, name, method, path, body) -> None:
    response = client.request(
        method, path, json=body, headers={"Authorization": f"Bearer {BAD_TOKENS[name]}"}
    )

    assert response.status_code == 401, name


def test_role_names_are_case_sensitive(client) -> None:
    token = sign(claims(roles=["Admin", "PLATFORM-ADMIN"]))

    audit = client.get("/audit", headers={"Authorization": f"Bearer {token}"})
    default_doc = client.post(
        "/documents/default",
        json={"title": "t", "content": "c", "allowed_roles": ["admin"]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert audit.status_code == 403 and default_doc.status_code == 403


def test_query_body_cannot_override_identity(client) -> None:
    seen: list[UserContext] = []

    class Answers:
        async def answer(self, question: str, user: UserContext) -> AnswerResult:
            seen.append(user)
            return AnswerResult("no", [], "no_results", None, None, None, 1.0, None)

    class Audit:
        async def record_answer(self, *args: object) -> None: ...

    app.dependency_overrides[get_answer_service] = lambda: Answers()
    app.dependency_overrides[get_audit_service] = lambda: Audit()

    response = client.post(
        "/query",
        json={
            "query": "q",
            "tenant_id": "globex",
            "roles": ["admin"],
            "user": {"tenant_id": "globex", "roles": ["platform-admin"]},
        },
        headers={"Authorization": f"Bearer {sign(claims())}"},
    )

    assert response.status_code == 200
    assert seen == [UserContext(user_id="bob", tenant_id="acme", roles=("employee",))]


def test_query_string_cannot_override_identity(client) -> None:
    seen: list[UserContext] = []

    class Answers:
        async def answer(self, question: str, user: UserContext) -> AnswerResult:
            seen.append(user)
            return AnswerResult("no", [], "no_results", None, None, None, 1.0, None)

    class Audit:
        async def record_answer(self, *args: object) -> None: ...

    app.dependency_overrides[get_answer_service] = lambda: Answers()
    app.dependency_overrides[get_audit_service] = lambda: Audit()

    client.post(
        "/query?tenant_id=globex&roles=admin",
        json={"query": "q"},
        headers={"Authorization": f"Bearer {sign(claims())}"},
    )

    assert [(u.tenant_id, u.roles) for u in seen] == [("acme", ("employee",))]

import pytest
from fastapi.testclient import TestClient

from app.auth.authentication import decode_user_context
from app.auth.dependencies import get_user_repository
from app.auth.passwords import hash_password
from app.auth.users import StoredUser
from app.config import Settings, get_settings
from app.main import app

settings = Settings(jwt_secret_key="test-secret-that-is-at-least-thirty-two-bytes")


class FakeUsers:
    def __init__(self, *users: StoredUser) -> None:
        self._users = {(user.tenant_id, user.username): user for user in users}

    async def find(self, tenant_id: str, username: str) -> StoredUser | None:
        return self._users.get((tenant_id, username))

    async def record_failed_login(self, user_id: str, max_attempts: int) -> bool:
        return False

    async def record_successful_login(self, user_id: str) -> bool:
        return True


def stored_user(*, active: bool = True, roles: tuple[str, ...] = ("employee",)) -> StoredUser:
    return StoredUser(
        id="7a1f0f4e-0000-4000-8000-000000000001",
        tenant_id="acme",
        username="bob",
        password_hash=hash_password("correct-horse"),
        is_active=active,
        roles=roles,
    )


@pytest.fixture
def client():
    def make(users: FakeUsers) -> TestClient:
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_user_repository] = lambda: users
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()


def login(client: TestClient, **overrides: str):
    body = {"tenant_id": "acme", "username": "bob", "password": "correct-horse", **overrides}
    return client.post("/auth/login", json=body)


def test_login_issues_token_with_server_side_identity(client) -> None:
    response = login(client(FakeUsers(stored_user(roles=("employee", "finance")))))

    assert response.status_code == 200
    user = decode_user_context(response.json()["access_token"], settings)
    assert (user.user_id, user.tenant_id) == ("7a1f0f4e-0000-4000-8000-000000000001", "acme")
    assert user.roles == ("employee", "finance")


@pytest.mark.parametrize(
    "overrides",
    [{"password": "wrong"}, {"username": "nobody"}, {"tenant_id": "globex"}],
)
def test_login_rejects_bad_credentials_with_identical_response(client, overrides) -> None:
    response = login(client(FakeUsers(stored_user())), **overrides)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid credentials"}


def test_login_rejects_inactive_user(client) -> None:
    assert login(client(FakeUsers(stored_user(active=False)))).status_code == 401


def test_login_body_cannot_smuggle_roles(client) -> None:
    response = login(client(FakeUsers(stored_user())), roles=["platform-admin"])

    user = decode_user_context(response.json()["access_token"], settings)
    assert user.roles == ("employee",)

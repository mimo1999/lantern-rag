"""Lockout, email password reset and token revocation, against real Postgres."""

import asyncio
import re

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth.authentication import decode_user_context
from app.auth.dependencies import get_user_repository
from app.auth.mail import get_mailer
from app.auth.passwords import hash_password
from app.auth.revocation import PostgresTokenRevocations, get_token_revocations
from app.auth.users import PostgresUserRepository
from app.config import get_settings
from app.main import app

PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "a-brand-new-passphrase"


class RecordingMailer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_password_reset(self, to: str, link: str) -> None:
        self.sent.append((to, link))

    def token(self) -> str:
        match = re.search(r"#reset=(\S+)$", self.sent[-1][1])
        assert match
        return match.group(1)


@pytest_asyncio.fixture
async def env(pg_pool: asyncpg.Pool, test_settings):
    await pg_pool.execute("INSERT INTO tenants (id, name) VALUES ('acme', 'Acme')")
    await pg_pool.execute(
        "INSERT INTO users (tenant_id, username, password_hash, email)"
        " VALUES ('acme', 'bob', $1, 'Bob@acme.example')",
        hash_password(PASSWORD),
    )
    mailer = RecordingMailer()
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_user_repository] = lambda: PostgresUserRepository(pg_pool)
    app.dependency_overrides[get_token_revocations] = lambda: PostgresTokenRevocations(pg_pool)
    app.dependency_overrides[get_mailer] = lambda: mailer
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    yield client, mailer, pg_pool
    app.dependency_overrides.clear()


async def login(client: AsyncClient, password: str = PASSWORD):
    return await client.post(
        "/auth/login", json={"tenant_id": "acme", "username": "bob", "password": password}
    )


async def request_reset(client: AsyncClient, email: str = "bob@acme.example"):
    return await client.post(
        "/auth/password-reset/request", json={"tenant_id": "acme", "email": email}
    )


async def confirm(client: AsyncClient, token: str, password: str = NEW_PASSWORD):
    return await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": password}
    )


@pytest.mark.asyncio
async def test_third_failed_login_locks_the_account_even_for_the_right_password(env) -> None:
    client, _, _ = env

    assert (await login(client, "wrong-1")).status_code == 401
    assert (await login(client, "wrong-2")).status_code == 401
    assert (await login(client, "wrong-3")).status_code == 423
    assert (await login(client)).status_code == 423


@pytest.mark.asyncio
async def test_successful_login_resets_the_failure_count(env) -> None:
    client, _, _ = env

    for _ in range(2):
        assert (await login(client, "wrong")).status_code == 401
    assert (await login(client)).status_code == 200
    for _ in range(2):
        assert (await login(client, "wrong")).status_code == 401  # not locked: count restarted


@pytest.mark.asyncio
async def test_parallel_guesses_cannot_exceed_the_attempt_limit(env) -> None:
    client, _, pool = env

    await asyncio.gather(*(login(client, f"guess-{i}") for i in range(10)))

    assert await pool.fetchval("SELECT locked_at IS NOT NULL FROM users") is True
    assert (await login(client)).status_code == 423


@pytest.mark.asyncio
async def test_reset_by_email_unlocks_sets_password_and_is_single_use(env) -> None:
    client, mailer, _ = env
    for _ in range(3):
        await login(client, "wrong")

    assert (await request_reset(client, "BOB@acme.example")).status_code == 202
    assert mailer.sent[0][0] == "BOB@acme.example"
    token = mailer.token()

    assert (await confirm(client, token)).status_code == 204
    assert (await login(client, PASSWORD)).status_code == 401  # old password is gone
    assert (await login(client, NEW_PASSWORD)).status_code == 200  # and the lock is cleared
    assert (await confirm(client, token, "yet-another-passphrase")).status_code == 400


@pytest.mark.asyncio
async def test_reset_request_does_not_reveal_unknown_emails_and_is_throttled(env) -> None:
    client, mailer, _ = env

    unknown = await request_reset(client, "nobody@acme.example")
    known = await request_reset(client)
    again = await request_reset(client)

    assert unknown.status_code == known.status_code == again.status_code == 202
    assert unknown.json() == known.json() == again.json()
    assert len(mailer.sent) == 1  # unknown sends nothing; the repeat within a minute is dropped


@pytest.mark.asyncio
async def test_expired_reset_token_is_rejected(env) -> None:
    client, mailer, pool = env
    await request_reset(client)
    await pool.execute("UPDATE password_resets SET expires_at = now() - interval '1 minute'")

    assert (await confirm(client, mailer.token())).status_code == 400


@pytest.mark.asyncio
async def test_reset_token_is_stored_hashed(env) -> None:
    client, mailer, pool = env
    await request_reset(client)

    stored = await pool.fetchval("SELECT token_hash FROM password_resets")

    assert stored != mailer.token() and mailer.token() not in stored


@pytest.mark.asyncio
async def test_logout_revokes_only_that_token(env, test_settings) -> None:
    client, _, _ = env
    first = (await login(client)).json()["access_token"]
    second = (await login(client)).json()["access_token"]
    assert decode_user_context(first, test_settings).token_id != (
        decode_user_context(second, test_settings).token_id
    )

    out = await client.post("/auth/logout", headers={"Authorization": f"Bearer {first}"})
    assert out.status_code == 204

    again = await client.post("/auth/logout", headers={"Authorization": f"Bearer {first}"})
    other = await client.post("/auth/logout", headers={"Authorization": f"Bearer {second}"})
    assert again.status_code == 401
    assert other.status_code == 204


@pytest.mark.asyncio
async def test_password_reset_revokes_tokens_issued_before_it(env) -> None:
    client, mailer, _ = env
    old = (await login(client)).json()["access_token"]
    await asyncio.sleep(1.1)  # token `iat` has one-second resolution
    await request_reset(client)
    await confirm(client, mailer.token())

    stale = await client.post("/auth/logout", headers={"Authorization": f"Bearer {old}"})
    fresh = (await login(client, NEW_PASSWORD)).json()["access_token"]
    ok = await client.post("/auth/logout", headers={"Authorization": f"Bearer {fresh}"})

    assert stale.status_code == 401
    assert ok.status_code == 204


@pytest.mark.asyncio
async def test_deactivated_user_token_stops_working(env) -> None:
    client, _, pool = env
    token = (await login(client)).json()["access_token"]
    await pool.execute("UPDATE users SET is_active = false")

    response = await client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401

"""Seed demo tenants, users and roles. Development only.

Run with: uv run python -m scripts.seed_db
All demo users share DEMO_PASSWORD; it exists only for local development data.
"""

import asyncio

import asyncpg

from app.auth.passwords import hash_password
from app.config import get_settings
from app.db.migrate import apply_migrations

DEMO_PASSWORD = "demo-password-123"

TENANTS = {"acme": "Acme Corp", "globex": "Globex Inc", "platform": "Platform Operators"}

# (tenant_id, username, roles); email is <username>@<tenant>.example
USERS = [
    ("acme", "alice", ["admin"]),
    ("acme", "bob", ["employee"]),
    ("acme", "carol", ["finance", "employee"]),
    ("globex", "dave", ["employee"]),
    ("globex", "erin", ["admin", "employee"]),
    ("platform", "root", ["platform-admin", "admin", "employee"]),
]


async def seed(conn: asyncpg.Connection) -> None:
    password_hash = hash_password(DEMO_PASSWORD)
    async with conn.transaction():
        for tenant_id, name in TENANTS.items():
            await conn.execute(
                "INSERT INTO tenants (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
                tenant_id,
                name,
            )
        for tenant_id, username, roles in USERS:
            user_id = await conn.fetchval(
                "INSERT INTO users (tenant_id, username, password_hash, email)"
                " VALUES ($1, $2, $3, $4)"
                " ON CONFLICT (tenant_id, username) DO UPDATE SET password_hash = $3,"
                " email = $4, failed_logins = 0, locked_at = NULL RETURNING id",
                tenant_id,
                username,
                password_hash,
                f"{username}@{tenant_id}.example",
            )
            await conn.execute("DELETE FROM user_roles WHERE user_id = $1", user_id)
            await conn.executemany(
                "INSERT INTO user_roles (user_id, role) VALUES ($1, $2)",
                [(user_id, role) for role in roles],
            )


async def _main() -> None:
    settings = get_settings()
    if settings.app_env != "development":
        raise SystemExit("Refusing to seed demo users outside APP_ENV=development")
    conn = await asyncpg.connect(**settings.postgres_connect_kwargs)
    try:
        await apply_migrations(conn)
        await seed(conn)
    finally:
        await conn.close()
    print(f"Seeded {len(TENANTS)} tenants and {len(USERS)} users.")


if __name__ == "__main__":
    asyncio.run(_main())

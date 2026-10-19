"""Forward-only SQL migrations. Run with: uv run python -m app.db.migrate"""

import asyncio
from pathlib import Path

import asyncpg

from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_ADVISORY_LOCK_KEY = 7_412_001


async def apply_migrations(conn: asyncpg.Connection) -> list[str]:
    """Apply pending migrations in filename order; returns the versions applied."""
    applied: list[str] = []
    async with conn.transaction():
        await conn.execute(f"SELECT pg_advisory_xact_lock({_ADVISORY_LOCK_KEY})")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        done = {row["version"] for row in await conn.fetch("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in done:
                continue
            await conn.execute(path.read_text(encoding="utf-8"))
            await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", path.stem)
            applied.append(path.stem)
    return applied


async def _main() -> None:
    conn = await asyncpg.connect(**get_settings().postgres_connect_kwargs)
    try:
        applied = await apply_migrations(conn)
    except (asyncpg.FeatureNotSupportedError, asyncpg.InsufficientPrivilegeError) as error:
        raise SystemExit(
            f"Migration failed: {error}. "
            "The pgvector extension must be installed on the server and created by a "
            "superuser (CREATE EXTENSION vector;). See README."
        ) from error
    finally:
        await conn.close()
    print("applied: " + (", ".join(applied) if applied else "nothing to do"))


if __name__ == "__main__":
    asyncio.run(_main())

# Repository Guidelines

## Project Structure & Module Organization

`permission_aware_multi_tenant_rag_implementation_plan.md` is the design and phase plan; `README.md` and `CLAUDE.md` describe the implemented architecture. Layout:

- `app/`: FastAPI service, one package per concern: `auth/` (JWT, passwords, users), `retrieval/` (authorization filter, pgvector retriever, embeddings), `ingestion/` (chunking, loaders, registry, indexer), `generation/` (LLM provider, prompts, answer service), `audit/`, `observability/`, `db/` (pool + SQL migrations), `api/` (routers), `web/` (static chat UI).
- `tests/unit/`, `tests/integration/` (real PostgreSQL in a throwaway schema; skipped if unavailable), and `tests/security/` (tenant, role, scope, spoofing, and prompt-injection invariants).
- `evaluation/`: adversarial security evaluation and retrieval-quality benchmark over `data/synthetic/`.
- `scripts/`: `seed_db` (demo tenants/users) and `ingest_documents` (demo corpus through the real stack).
- `deploy/`: Prometheus configuration for Compose.
- Root configuration: `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `.env.example`, and `README.md`.

When adding code, keep authorization policy construction separate from retrieval and generation. The non-negotiable flow is identity → authorization → filtered retrieval → context → LLM.

## Build, Test, and Development Commands

```text
uv sync --all-groups                        # install
uv run python -m app.db.migrate             # apply SQL migrations
uv run python -m scripts.seed_db            # demo tenants and users (development only)
uv run uvicorn app.main:app --reload        # landing page at /, chat app at /app/
uv run pytest                               # complete test suite
uv run ruff check .                         # lint
uv run python -m evaluation.security_eval   # adversarial leakage evaluation (must report 0%)
uv run python -m evaluation.retrieval_eval  # retrieval quality (--embedder ollama for the real model)
docker compose up --build                   # full local stack
```

Run ruff only over this project's own directories.

## Coding Style & Naming Conventions

Use Python 3.12+, four-space indentation, type hints, small modules, and async I/O at API boundaries. Format and lint with the tools selected in `pyproject.toml`. Use `snake_case` for modules, functions, and variables; `PascalCase` for classes; and descriptive names such as `build_authorization_filter`. Never accept tenant IDs or roles from the query body; derive them from verified identity and server-side policy.

## Testing Guidelines

Use `pytest`. Name files `test_*.py` and group security invariants under `tests/security/`. Every change affecting retrieval should cover tenant isolation, role isolation, privilege/tenant spoofing, and the guarantee that unauthorized chunks never reach the LLM context. Target zero cross-tenant leakage and zero unauthorized retrievals.

## Commit & Pull Request Guidelines

Git history is not available in the current checkout, so no established commit convention can be inferred. Use concise imperative commits (for example, `Add tenant-scoped vector filter`). Pull requests should explain the security impact, include tests and verification commands, link any issue, and include screenshots or trace links when changing observability or demo behavior.

## Security & Configuration

Copy `.env.example` to a local environment file; never commit secrets, passwords, JWT keys, tokens, or unrestricted document contents. Keep Postgres internal-only. Prefer chunk/document IDs and redacted metadata in logs and traces over raw sensitive text.

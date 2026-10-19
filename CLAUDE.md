# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

Lantern: a permission-aware, multi-tenant RAG service: FastAPI, PostgreSQL with pgvector (users, document registry, audit, and the vectors themselves), Ollama (embeddings and generation), and a static chat UI. The design and phase plan is [permission_aware_multi_tenant_rag_implementation_plan.md](permission_aware_multi_tenant_rag_implementation_plan.md); its "Implementation status" block lists the deliberate deviations and what is still unverified. [README.md](README.md) carries the threat model and run instructions.

Not built (TODO): a reranker, deliberately deferred until scale makes vector top-k noisy (needs a model-serving decision). Verified live end to end: PostgreSQL with pgvector 0.8.6, real embeddings from `all-minilm:22m` (384 dimensions), and the chat model `gemma4:e2b` (ingest, filtered retrieval, cited answers, isolation across tenants; Recall@1 0.947 and MRR 0.974 on the 19-query set). `docker compose up` is verified end to end with the host Ollama (default); the `bundled-ollama` profile (own Ollama container, downloads models) is not. There is no tracing (OpenTelemetry was removed as unused until a collector exists). Tests need pgvector and skip without it — check `pytest -rs` before trusting a green run.

Dev database: the local Windows PostgreSQL 18 has no pgvector, so development uses Docker container `rag-pgvector` (image `pgvector/pgvector:pg17`, volume `rag_pgvector_data`) published on `127.0.0.1:5433`; `.env` points there. Use `POSTGRES_HOST=127.0.0.1`, not `localhost`: Windows tries IPv6 first and every new connection then stalls ~2s (a 10x test slowdown).

If you change `EMBEDDING_MODEL`/`EMBEDDING_DIMENSION`, add a migration that changes `document_chunks.embedding` (its `vector(384)` type fixes the dimension; the old chunks cannot be converted and must be re-embedded) and re-embed everything — vectors from different embedding models must never share a collection.

## Commands

```bash
cp .env.example .env                        # set POSTGRES_PASSWORD and a real JWT_SECRET_KEY
uv sync --all-groups
uv run python -m app.db.migrate             # forward-only SQL migrations
uv run python -m scripts.seed_db            # demo tenants/users; refuses unless APP_ENV=development
uv run python -m scripts.ingest_documents   # demo corpus through Postgres/pgvector + Ollama
uv run uvicorn app.main:app --reload        # landing page at /, chat app at /app/, on http://127.0.0.1:8000
uv run pytest                               # full suite (integration tests need Postgres, else skipped)
uv run pytest -m slow                       # real Docling conversion of tests/fixtures/report.pdf (~30 s, excluded by default)
uv run pytest tests/security/test_spoofing.py::test_name   # single test
uv run ruff check .                         # lint (never pass --fix over vendored folders)
uv run python -m evaluation.security_eval   # adversarial leakage suite; exits non-zero on any leak
uv run python -m evaluation.retrieval_eval  # Recall/Precision/MRR/NDCG (--embedder ollama for the real model)
uv run python -m evaluation.table_eval      # table serialization comparison (markdown/csv/sentences) with bge-m3
docker compose up --build                   # full stack
```

## Request flow

`POST /query`: JWT → `UserContext` → `AuthorizationFilter` → embed query → one pgvector SQL (`embedding_model` + filter, cosine order) → relative cutoff → numbered, escaped, untrusted context → `gemma4:e2b` (skipped if nothing retrieved) → `audit_log` row (503 if the write fails) → answer + `sources`.

`DELETE /documents/{id}` (admin): `DELETE FROM documents WHERE id AND tenant` (default scope only for platform-admin); chunks go by `ON DELETE CASCADE`; anything else is 404.

`POST /documents/upload` (admin): validate type/size/roles → `IngestionService.start` registers an `indexing` row → `202` with the id → background task `uploads.process_upload`: extract (Docling for PDFs, with limits) → `complete`: duplicate check → caption images → chunk (1200/150) → embed → one transaction writing chunks with `tenant_id`, `scope`, `allowed_roles`, `embedding_model` → row `ready` / `failed` (fixed message) / `duplicate`. Clients poll `GET /documents/{id}/status`; `GET /documents/uploads` lists unfinished ones. 3 in flight per tenant; jobs are in-process (a restart abandons them, rows older than 30 min read as failed).

Both paths use the same `document_chunks` table; the diagram is in [README.md](README.md#architecture).

## Architecture

The non-negotiable request flow is **identity → authorization → filtered retrieval → context → LLM**. Authorization policy construction stays separate from retrieval and generation. Each concern is its own package behind a small typed interface, so features can change independently.

1. **Identity** — [app/auth](app/auth): `authentication.py` validates the bearer JWT (pinned algorithm, issuer, required `exp`/`sub`/`tenant_id`/`roles`) into a `UserContext`; every failure is a generic 401. `require_role(role)` gates endpoints (403); `PLATFORM_ADMIN_ROLE` is distinct from a tenant's own `admin`. `POST /auth/login` (`users.py`, `passwords.py`, `tokens.py`) checks Argon2 hashes with a constant-cost path for unknown users and issues the token; the login body's `tenant_id` only selects a username namespace. Account security lives beside it: 3 failed logins lock the account (atomic SQL counter, 423) until a single-use emailed reset (`/auth/password-reset/*`, `mail.py`; token stored as SHA-256) unlocks it; every token carries a `jti`, `get_current_user` rejects revoked ones via `revocation.py` (logout denylist, deactivated user, `tokens_valid_after` set by a reset) and applies the per-tenant limiter in `app/ratelimit.py` (429). Tests that forge tokens must include `jti` and `iat`, or they would pass for the wrong reason.
2. **Authorization filter** — [app/retrieval/filters.py](app/retrieval/filters.py) is the only place scope/tenant/role scoping is built: `(scope == default OR tenant_id == user.tenant_id) AND allowed_roles ∩ user.roles`, as a nested `Filter(should=[...])` inside `must`. Nothing else may build ad hoc filters.
3. **Retrieval** — [app/retrieval](app/retrieval): `RetrievalService` always applies the filter before calling the `Retriever` protocol, then applies relevance cutoffs (`RETRIEVAL_RELATIVE_CUTOFF`, 0.4 of the best score; off in the constructor, on in `Settings`) strictly after authorization, so it can only remove already-authorized chunks — the security evaluation deliberately runs with it off for worst-case exposure; `PgVectorRetriever` embeds the query and runs one SQL statement: `WHERE embedding_model = $model AND <authorization filter> ORDER BY embedding <=> $query LIMIT k`. `ensure_ready()` fails fast on a missing extension or a dimension mismatch. Chunk inserts are one transaction, and `document_chunks.document_id` cascades from `documents`. `RetrievalService.list_documents` reuses the same filter for `GET /documents`. `RetrievedChunk` carries `title`/`scope`/`score` for the trace. `OllamaEmbedder` validates vector dimension; `HashingEmbedder` is an offline lexical embedder for tests and evaluation.
4. **Ingestion** — [app/ingestion](app/ingestion): `loaders.extract_document` handles `.md`/`.txt` and `.pdf` (Docling, lazily imported, CPU, OCR off, 100 pages, 5 min) from untrusted bytes and returns prose plus tables (markdown) and images (PNG); `chunker` splits prose; `captioner.OllamaCaptioner` (vision model) turns each image into text after the duplicate check; `DocumentIndexer` embeds prose with the text model and tables/captions with the asset model (`bge-m3`, `vector(1024)`) and writes `document_chunks` and `asset_chunks` in one transaction; `app/retrieval/combined.py` searches both tables with the same authorization filter and `RetrievalService` cuts and ranks per group (prose vs assets) by share of the group's best score, so assets are not starved by the context budget; `IngestionService` orchestrates registry row → index → mark ready/failed, with checksum de-duplication that includes the role set; `DocumentForIndexing.__post_init__` enforces tenant-scope ⇔ `tenant_id`. `PostgresDocumentRegistry` tracks status.
5. **Generation** — [app/generation](app/generation): `AnswerService` retrieves, skips the LLM when nothing is retrievable, trims context to `MAX_CONTEXT_CHARS`, and calls an `LLMProvider` (`OllamaChatProvider`). `prompts.py` escapes `<`/`>` in document text and titles, delimits context, and declares it untrusted. `sources` in the response is exactly the chunks placed in the prompt.
6. **Audit** — [app/audit](app/audit): every `/query` writes an `audit_log` row (`hash` of the query, chunk ids and scores, models, latencies, outcome). A failed audit write raises `AuditError` and the request fails with 503 (fail closed). `GET /audit` is admin-only and forced to the caller's tenant unless the caller is `platform-admin`. The table is append-only via a trigger.
7. **Observability** — [app/observability](app/observability): pure-ASGI middleware assigns/validates a UUID request id, emits one JSON log line, and records Prometheus HTTP metrics with route templates (never raw paths). `/metrics` honors `METRICS_TOKEN`. Logs and metric labels carry ids and counts only — never query text, chunk text, or tenant/user metric labels.
8. **Persistence** — [app/db](app/db): asyncpg pool (psycopg was rejected: its async mode fails on Windows' default event loop), forward-only migrations in `app/db/migrations/*.sql` run by `python -m app.db.migrate` (Compose runs it before uvicorn).
9. **API and UI** — [app/api](app/api) routers; `app/main.py` mounts the static UI from [app/web](app/web) last, so API routes win. `app/web/` holds the public landing page (`index.html`, `landing.css`, `landing.js`: a restrained light/dark page with one vermilion accent, whose "who can retrieve what" explorer is a real component over sample data that only illustrates the server's rule) and, under `app/web/app/`, the chat app (served at `/app/`, older teal look), with a strict CSP on UI files only. The UI builds DOM with `textContent` (a test forbids `innerHTML`) and contains no authorization logic.

Dependencies are wired with FastAPI `Depends` per feature (`app/*/dependencies.py`); tests override them. `app/dependencies.py` builds the cached `PgVectorRetriever`.

Tenant, scope, and roles must always come from the verified `UserContext`, never from request bodies, query strings, or form fields.

## Testing

`tests/security/` holds the invariants and is the priority to extend when touching auth, retrieval, or ingestion: tenant isolation, role isolation, default-vs-tenant scope, spoofing (forged/`alg: none`/expired tokens, body and query-string identity), prompt injection, and that unauthorized text never reaches the LLM prompt or the `GET /documents` listing. `test_security_eval.py` runs the adversarial suite and also proves it fails when tenant isolation is sabotaged. `tests/integration/` uses real PostgreSQL in a per-test throwaway schema (`isolated_pool` in `evaluation/db.py`): the `pg_pool` fixture needs pgvector and skips without it. `tests/integration/test_filter_sql.py` runs the real authorization predicate against a plain table. `evaluation/testing.py` provides `VectorEnv` (readable document names over UUID ids). `tests/unit/` uses `httpx.MockTransport` for Ollama. Any change to `filters.py`, `service.py`, `pgvector.py`, `indexer.py` scope validation, or prompt construction needs a matching security test.

## Security & configuration

- Copy `.env.example` to `.env`; never commit secrets, JWT keys, tokens, or raw sensitive document content. Outside `APP_ENV=development|test` the app refuses a short or placeholder `JWT_SECRET_KEY`.
- Keep Postgres internal-only (Compose does not publish it).
- Prefer chunk/document ids and redacted metadata in logs and traces over raw sensitive text.

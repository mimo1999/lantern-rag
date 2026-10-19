"""Shared fixtures for the evaluation suites: the synthetic corpus, its users, an
independent authorization oracle, and a pgvector-backed retrieval stack."""

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import asyncpg

from app.auth.models import UserContext
from app.ingestion.indexer import DocumentForIndexing, DocumentIndexer, DocumentScope, IndexResult
from app.retrieval.embeddings import Embedder, HashingEmbedder
from app.retrieval.pgvector import PgVectorRetriever
from app.retrieval.service import RetrievalService

SYNTHETIC_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic"
EVAL_DIMENSION = 384  # matches the vector(384) column

USERS = {
    "alice": UserContext("alice", "acme", ("admin",)),
    "bob": UserContext("bob", "acme", ("employee",)),
    "carol": UserContext("carol", "acme", ("finance", "employee")),
    "dave": UserContext("dave", "globex", ("employee",)),
    "erin": UserContext("erin", "globex", ("admin", "employee")),
}


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    key: str  # readable id such as "acme-finance"; used by datasets and reports
    document_id: str  # deterministic UUID stored in the database
    file: str
    scope: DocumentScope
    tenant_id: str | None
    title: str
    allowed_roles: tuple[str, ...]
    canary: str
    text: str


def load_corpus() -> list[CorpusDocument]:
    manifest = json.loads((SYNTHETIC_DIR / "manifest.json").read_text(encoding="utf-8"))
    return [
        CorpusDocument(
            key=entry["file"].replace("/", "-").removesuffix(".md"),
            document_id=str(uuid5(NAMESPACE_URL, entry["file"])),
            file=entry["file"],
            scope=entry["scope"],
            tenant_id=entry["tenant_id"],
            title=entry["title"],
            allowed_roles=tuple(entry["allowed_roles"]),
            canary=entry["canary"],
            text=(SYNTHETIC_DIR / entry["file"]).read_text(encoding="utf-8"),
        )
        for entry in manifest["documents"]
    ]


def is_authorized(user: UserContext, document: CorpusDocument) -> bool:
    """Ground truth, written independently of app.retrieval.filters on purpose."""
    in_scope = document.scope == "default" or document.tenant_id == user.tenant_id
    return in_scope and bool(set(user.roles) & set(document.allowed_roles))


async def register_and_index(
    pool: asyncpg.Pool, indexer: DocumentIndexer, document: DocumentForIndexing
) -> IndexResult:
    """Create the tenant and registry rows the chunks reference, then index (test/eval helper)."""
    if document.tenant_id is not None:
        await pool.execute(
            "INSERT INTO tenants (id, name) VALUES ($1, $1) ON CONFLICT (id) DO NOTHING",
            document.tenant_id,
        )
    await pool.execute(
        "INSERT INTO documents (id, scope, tenant_id, title, checksum, allowed_roles, status,"
        " created_by) VALUES ($1::uuid, $2, $3, $4, $1, $5, 'ready', 'eval')"
        " ON CONFLICT (id) DO NOTHING",
        document.document_id,
        document.scope,
        document.tenant_id,
        document.title,
        list(document.allowed_roles),
    )
    return await indexer.index(document)


async def build_stack(
    pool: asyncpg.Pool,
    embedder: Embedder | None = None,
    relative_cutoff: float = 0.0,
) -> tuple[RetrievalService, list[CorpusDocument]]:
    """Index the corpus into pgvector using the production indexer and retriever."""
    retriever = PgVectorRetriever(pool, embedder or HashingEmbedder(EVAL_DIMENSION))
    indexer = DocumentIndexer(retriever)
    corpus = load_corpus()
    for document in corpus:
        await register_and_index(
            pool,
            indexer,
            DocumentForIndexing(
                document_id=document.document_id,
                scope=document.scope,
                tenant_id=document.tenant_id,
                title=document.title,
                text=document.text,
                allowed_roles=document.allowed_roles,
            ),
        )
    return RetrievalService(retriever, 20, relative_cutoff), corpus

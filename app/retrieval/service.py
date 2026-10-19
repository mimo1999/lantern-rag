from dataclasses import dataclass
from typing import Protocol

from app.auth.models import UserContext
from app.retrieval.filters import AuthorizationFilter, build_authorization_filter


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    document_id: str
    chunk_id: str
    title: str
    scope: str
    text: str
    score: float
    kind: str = "text"  # "table" and "image" come from a different embedding model


@dataclass(frozen=True, slots=True)
class DocumentSummary:
    document_id: str
    title: str
    scope: str
    tenant_id: str | None
    source_uri: str | None
    allowed_roles: tuple[str, ...]


class Retriever(Protocol):
    async def search(
        self, query: str, authorization_filter: AuthorizationFilter, limit: int
    ) -> list[RetrievedChunk]: ...

    async def list_documents(
        self, authorization_filter: AuthorizationFilter
    ) -> list[DocumentSummary]: ...


class RetrievalService:
    """Authorized search. Every read builds its filter from the verified user.

    `relative_cutoff` (0 = off) drops chunks scoring below that fraction of the best chunk.
    It runs after authorization, so it can only remove chunks the caller could already see.
    Relative rather than absolute because relevant and irrelevant scores overlap across
    queries (a vague query's best hit can score lower than an off-topic query's), while the
    tail behind a query's own best hit is reliably weaker.
    """

    def __init__(self, retriever: Retriever, top_k: int, relative_cutoff: float = 0.0) -> None:
        self._retriever = retriever
        self._top_k = top_k
        self._relative_cutoff = relative_cutoff

    async def retrieve(self, query: str, user: UserContext) -> list[RetrievedChunk]:
        chunks = await self._retriever.search(query, build_authorization_filter(user), self._top_k)
        if not chunks:
            return chunks
        # Prose and tables/images are scored by different models, so scores are only comparable
        # within a group. Each chunk is therefore ranked and cut relative to its own group's
        # best score. Ranking by that share interleaves the groups; appending assets after the
        # prose would let ten prose chunks fill the context budget and drop a table that was
        # the single best match. Within one group the order is unchanged.
        best: dict[bool, float] = {}
        for chunk in chunks:
            is_text = chunk.kind == "text"
            best[is_text] = max(best.get(is_text, chunk.score), chunk.score)

        def share(chunk: RetrievedChunk) -> float:
            top = best[chunk.kind == "text"]
            return chunk.score / top if top > 0 else chunk.score

        ranked = sorted(chunks, key=share, reverse=True)  # stable: ties keep search order
        if self._relative_cutoff <= 0:
            return ranked
        return [
            chunk
            for chunk in ranked
            if chunk.score >= self._relative_cutoff * best[chunk.kind == "text"]
        ]

    async def list_documents(self, user: UserContext) -> list[DocumentSummary]:
        """Same filter as `retrieve`, so a listing never reveals an unretrievable document."""
        return await self._retriever.list_documents(build_authorization_filter(user))

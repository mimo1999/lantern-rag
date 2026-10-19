import asyncio

from app.retrieval.filters import AuthorizationFilter
from app.retrieval.service import DocumentSummary, RetrievedChunk, Retriever


class CombinedRetriever:
    """Prose chunks plus table/image chunks, each searched in its own vector table with its own
    embedding model. Both searches receive the same authorization filter.

    Scores from different models are not comparable, so results are returned as two lists
    (prose first); `RetrievalService` cuts and ranks them per group.
    """

    def __init__(self, text: Retriever, assets: Retriever, asset_limit: int) -> None:
        self._text = text
        self._assets = assets
        self._asset_limit = asset_limit

    async def search(
        self, query: str, authorization_filter: AuthorizationFilter, limit: int
    ) -> list[RetrievedChunk]:
        text, assets = await asyncio.gather(
            self._text.search(query, authorization_filter, limit),
            self._assets.search(query, authorization_filter, self._asset_limit),
        )
        return [*text, *assets]

    async def list_documents(
        self, authorization_filter: AuthorizationFilter
    ) -> list[DocumentSummary]:
        text, assets = await asyncio.gather(
            self._text.list_documents(authorization_filter),
            self._assets.list_documents(authorization_filter),
        )
        unique = {summary.document_id: summary for summary in [*assets, *text]}
        return sorted(unique.values(), key=lambda summary: summary.title)

import pytest
from pydantic import ValidationError

from app.auth.models import UserContext
from app.config import Settings
from app.retrieval.filters import AuthorizationFilter
from app.retrieval.service import RetrievalService, RetrievedChunk

USER = UserContext("bob", "acme", ("employee",))


class FakeRetriever:
    def __init__(self, scores: list[float]) -> None:
        self._scores = scores
        self.filters: list[AuthorizationFilter] = []

    async def search(
        self, query: str, authorization_filter: AuthorizationFilter, limit: int
    ) -> list[RetrievedChunk]:
        self.filters.append(authorization_filter)
        return [
            RetrievedChunk("d", f"c{i}", f"Doc {i}", "tenant", "text", score)
            for i, score in enumerate(self._scores)
        ][:limit]


async def scores_after(scores: list[float], **cutoffs: float) -> list[float]:
    service = RetrievalService(FakeRetriever(scores), top_k=10, **cutoffs)
    return [chunk.score for chunk in await service.retrieve("q", USER)]


@pytest.mark.asyncio
async def test_cutoff_is_off_by_default() -> None:
    assert await scores_after([0.9, 0.3, 0.01, -0.2]) == [0.9, 0.3, 0.01, -0.2]


@pytest.mark.asyncio
async def test_relative_cutoff_drops_the_tail_but_never_the_best_hit() -> None:
    assert await scores_after([0.8, 0.5, 0.39, 0.1], relative_cutoff=0.5) == [0.8, 0.5]
    assert await scores_after([0.11, -0.05, -0.06], relative_cutoff=0.4) == [0.11]


@pytest.mark.asyncio
async def test_cutoff_boundaries_are_inclusive() -> None:
    assert await scores_after([1.0, 0.5], relative_cutoff=0.5) == [1.0, 0.5]


@pytest.mark.asyncio
async def test_cutoffs_handle_an_empty_result_set() -> None:
    assert await scores_after([], relative_cutoff=0.5) == []


@pytest.mark.asyncio
async def test_cutoffs_never_bypass_the_authorization_filter() -> None:
    retriever = FakeRetriever([0.9])
    service = RetrievalService(retriever, 10)

    await service.retrieve("q", USER)

    assert retriever.filters == [AuthorizationFilter(tenant_id="acme", roles=("employee",))]


@pytest.mark.parametrize("value", [-0.1, 1.5])
def test_settings_reject_out_of_range_cutoff(value: float) -> None:
    with pytest.raises(ValidationError, match="between 0 and 1"):
        Settings(retrieval_relative_cutoff=value)


def test_shipped_default_is_the_measured_value() -> None:
    assert Settings().retrieval_relative_cutoff == 0.4

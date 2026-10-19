import pytest

from app.retrieval.embeddings import HashingEmbedder


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


@pytest.mark.asyncio
async def test_embeds_real_words_into_unit_vectors_where_related_text_is_closer() -> None:
    embedder = HashingEmbedder(256)

    query = await embedder.embed("vacation days per year")
    related = await embedder.embed("How many vacation days do employees get")
    unrelated = await embedder.embed("kubernetes deployment pipeline")

    assert any(query), "words must produce a non-zero vector"
    assert abs(dot(query, query) - 1.0) < 1e-9
    assert dot(query, related) > dot(query, unrelated)


@pytest.mark.asyncio
async def test_empty_text_embeds_to_zero_vector_without_error() -> None:
    assert not any(await HashingEmbedder(8).embed("   "))

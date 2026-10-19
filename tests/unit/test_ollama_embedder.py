import json

import httpx
import pytest

from app.retrieval.embeddings import EmbeddingError, OllamaEmbedder


@pytest.mark.asyncio
async def test_ollama_embedder_sends_batched_request_and_validates_dimension() -> None:
    seen_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

    embedder = OllamaEmbedder(
        "http://ollama:11434",
        "qwen3-embedding:0.6b",
        dimension=2,
        transport=httpx.MockTransport(handler),
    )

    vectors = await embedder.embed_many(["first text", "second text"])

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert seen_request is not None
    assert seen_request.url.path == "/api/embed"
    assert seen_request.content == (
        b'{"model":"qwen3-embedding:0.6b","input":["first text","second text"],"truncate":true}'
    )


@pytest.mark.asyncio
async def test_ollama_embedder_rejects_wrong_vector_dimension() -> None:
    embedder = OllamaEmbedder(
        "http://ollama:11434",
        "qwen3-embedding:0.6b",
        dimension=2,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"embeddings": [[1.0]]})
        ),
    )

    with pytest.raises(EmbeddingError, match="dimension"):
        await embedder.embed("text")


@pytest.mark.asyncio
async def test_embed_many_splits_large_inputs_into_bounded_requests() -> None:
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        batch = json.loads(request.content)["input"]
        sizes.append(len(batch))
        return httpx.Response(200, json={"embeddings": [[0.5, 0.5] for _ in batch]})

    embedder = OllamaEmbedder(
        "http://ollama:11434", "all-minilm:22m", dimension=2, transport=httpx.MockTransport(handler)
    )

    vectors = await embedder.embed_many([f"chunk {i}" for i in range(130)])

    assert sizes == [64, 64, 2] and len(vectors) == 130
    assert await embedder.embed_many([]) == []

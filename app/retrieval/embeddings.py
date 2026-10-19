import hashlib
import math
import re
from typing import Protocol

import httpx

_BATCH_SIZE = 64


class Embedder(Protocol):
    dimension: int
    model: str  # recorded on every chunk so vectors from different models never mix

    async def embed(self, text: str) -> list[float]: ...

    async def embed_many(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingError(RuntimeError):
    """Raised when an embedding provider returns an unusable response."""


class OllamaEmbedder:
    """Embed text using Ollama's native batch embedding endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        dimension: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self.model = model
        self.dimension = dimension
        self._transport = transport

    async def embed(self, text: str) -> list[float]:
        return (await self.embed_many([text]))[0]

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            vectors.extend(await self._embed_batch(texts[start : start + _BATCH_SIZE]))
        return vectors

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(60.0, connect=5.0),
                transport=self._transport,
            ) as client:
                response = await client.post(
                    "/api/embed",
                    json={"model": self.model, "input": texts, "truncate": True},
                )
                response.raise_for_status()
        except httpx.HTTPError as error:
            raise EmbeddingError("Ollama embedding request failed") from error

        try:
            embeddings = response.json()["embeddings"]
        except (KeyError, TypeError, ValueError) as error:
            raise EmbeddingError("Ollama embedding response did not contain embeddings") from error
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise EmbeddingError("Ollama returned an unexpected number of embeddings")

        vectors: list[list[float]] = []
        for vector in embeddings:
            if (
                not isinstance(vector, list)
                or len(vector) != self.dimension
                or not all(isinstance(value, int | float) for value in vector)
            ):
                raise EmbeddingError(
                    f"Ollama vector dimension does not match configured dimension {self.dimension}"
                )
            vectors.append([float(value) for value in vector])
        return vectors


class HashingEmbedder:
    """Deterministic local baseline; replace with a model embedder for production quality."""

    def __init__(self, dimension: int) -> None:
        if dimension <= 0:
            raise ValueError("Embedding dimension must be positive")
        self.dimension = dimension
        self.model = f"hashing-{dimension}"

    async def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in re.findall(r"[\w-]+", text.lower()):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            slot = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[slot] += sign
        magnitude = math.sqrt(sum(value * value for value in vector))
        return vector if magnitude == 0 else [value / magnitude for value in vector]

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(text) for text in texts]

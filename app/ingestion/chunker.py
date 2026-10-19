from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TextChunk:
    index: int
    text: str


def chunk_text(text: str, chunk_size: int = 2_000, overlap: int = 250) -> list[TextChunk]:
    """Split normalized text on whitespace while preserving a configurable overlap."""
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("chunk_size must be positive and overlap must be smaller than chunk_size")

    normalized = " ".join(text.split())
    if not normalized:
        return []

    chunks: list[TextChunk] = []
    start = 0
    index = 0
    while start < len(normalized):
        end = min(start + chunk_size, len(normalized))
        if end < len(normalized):
            boundary = normalized.rfind(" ", start, end)
            if boundary > start:
                end = boundary
        chunks.append(TextChunk(index=index, text=normalized[start:end]))
        if end == len(normalized):
            break
        start = end - overlap
        while start < len(normalized) and normalized[start] == " ":
            start += 1
        index += 1
    return chunks

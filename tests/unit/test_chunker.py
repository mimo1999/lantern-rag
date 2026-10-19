from app.ingestion.chunker import chunk_text


def test_chunker_preserves_text_with_overlap() -> None:
    chunks = chunk_text("alpha beta gamma delta epsilon", chunk_size=16, overlap=5)

    assert [chunk.text for chunk in chunks] == [
        "alpha beta",
        "beta gamma",
        "gamma delta",
        "delta epsilon",
    ]

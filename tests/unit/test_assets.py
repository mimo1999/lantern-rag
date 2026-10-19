import base64
import json

import httpx
import pytest

from app.auth.models import UserContext
from app.generation.prompts import build_messages
from app.ingestion.captioner import CaptionError, OllamaCaptioner
from app.ingestion.indexer import DocumentForIndexing, IndexResult
from app.ingestion.loaders import ExtractedImage, ExtractedTable
from app.ingestion.service import IngestionService, NewDocument, content_checksum
from app.retrieval.combined import CombinedRetriever
from app.retrieval.filters import AuthorizationFilter
from app.retrieval.service import DocumentSummary, RetrievalService, RetrievedChunk

USER = UserContext("bob", "acme", ("employee",))


def chunk(chunk_id: str, score: float, kind: str = "text", doc: str = "d") -> RetrievedChunk:
    return RetrievedChunk(doc, chunk_id, f"Doc {doc}", "tenant", "text", score, kind)


class FakeRetriever:
    def __init__(self, chunks: list[RetrievedChunk], docs: list[DocumentSummary] = ()) -> None:
        self._chunks = chunks
        self._docs = list(docs)
        self.filters: list[AuthorizationFilter] = []
        self.limits: list[int] = []

    async def search(self, query, authorization_filter, limit):
        self.filters.append(authorization_filter)
        self.limits.append(limit)
        return self._chunks[:limit]

    async def list_documents(self, authorization_filter):
        self.filters.append(authorization_filter)
        return self._docs


@pytest.mark.asyncio
async def test_cutoff_is_relative_to_each_models_own_best_score() -> None:
    # Asset-model scores run higher than minilm's; a shared floor would wipe out the prose.
    text = [chunk("t0", 0.5), chunk("t1", 0.4), chunk("t2", 0.1)]
    assets = [chunk("a0", 0.9, "table"), chunk("a1", 0.8, "image"), chunk("a2", 0.2, "table")]
    service = RetrievalService(
        CombinedRetriever(FakeRetriever(text), FakeRetriever(assets), 3), 10, 0.4
    )

    kept = [c.chunk_id for c in await service.retrieve("q", USER)]

    assert kept == ["t0", "a0", "a1", "t1"]  # ranked by share of each group's best


@pytest.mark.asyncio
async def test_both_searches_receive_the_same_authorization_filter() -> None:
    text, assets = FakeRetriever([chunk("t0", 0.5)]), FakeRetriever([chunk("a0", 0.5, "table")])
    combined = CombinedRetriever(text, assets, asset_limit=2)

    results = await RetrievalService(combined, top_k=7).retrieve("q", USER)

    assert [c.kind for c in results] == ["text", "table"]  # prose first
    assert text.filters == assets.filters and len(text.filters) == 1
    assert text.filters[0].tenant_id == "acme" and text.filters[0].roles == ("employee",)
    assert (text.limits, assets.limits) == ([7], [2])


@pytest.mark.asyncio
async def test_listing_merges_both_tables_without_duplicates() -> None:
    def summary(doc_id: str, title: str) -> DocumentSummary:
        return DocumentSummary(doc_id, title, "tenant", "acme", None, ("admin",))

    both, only_assets, only_text = summary("1", "B"), summary("2", "A"), summary("3", "C")
    combined = CombinedRetriever(
        FakeRetriever([], [both, only_text]), FakeRetriever([], [both, only_assets]), 3
    )

    listed = await combined.list_documents(AuthorizationFilter("acme", ("admin",)))

    assert [d.title for d in listed] == ["A", "B", "C"]


def test_prompt_labels_tables_and_images_but_not_prose() -> None:
    chunks = [chunk("t", 0.5), chunk("a", 0.5, "table"), chunk("i", 0.5, "image")]

    prompt = build_messages("q", chunks)[1].content

    assert '<document id="1" title="Doc d">' in prompt
    assert '<document id="2" title="Doc d" kind="table">' in prompt
    assert '<document id="3" title="Doc d" kind="image">' in prompt


@pytest.mark.asyncio
async def test_captioner_sends_the_image_and_returns_the_caption() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "  A bar chart of revenue.  "}})

    captioner = OllamaCaptioner("http://ollama", "vision", transport=httpx.MockTransport(handler))

    assert await captioner.caption(b"\x89PNGdata") == "A bar chart of revenue."
    assert seen["model"] == "vision" and seen["stream"] is False
    assert base64.b64decode(seen["messages"][0]["images"][0]) == b"\x89PNGdata"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, json={"message": {"content": " "}}),
        httpx.Response(200, text="x"),
    ],
)
async def test_captioner_failures_are_caption_errors(response) -> None:
    captioner = OllamaCaptioner(
        "http://ollama", "v", transport=httpx.MockTransport(lambda r: response)
    )

    with pytest.raises(CaptionError):
        await captioner.caption(b"png")


class RecordingIndexer:
    def __init__(self) -> None:
        self.documents: list[DocumentForIndexing] = []

    async def index(self, document: DocumentForIndexing) -> IndexResult:
        self.documents.append(document)
        return IndexResult(
            2,
            tables=sum(a.kind == "table" for a in document.assets),
            images=sum(a.kind == "image" for a in document.assets),
        )


class FakeRegistry:
    async def find_ready_duplicate(self, *args):
        return None

    async def create(self, document):
        pass

    async def mark_ready(self, *args):
        pass

    async def mark_failed(self, *args):
        pass


class FlakyCaptioner:
    async def caption(self, png: bytes) -> str:
        if png == b"bad":
            raise CaptionError("no")
        return f"caption of {png.decode()}"


@pytest.mark.asyncio
async def test_ingest_indexes_tables_and_captions_and_skips_images_it_cannot_describe() -> None:
    indexer = RecordingIndexer()
    service = IngestionService(FakeRegistry(), indexer, FlakyCaptioner())

    result = await service.ingest(
        NewDocument(
            scope="tenant",
            tenant_id="acme",
            title="Report",
            text="prose",
            allowed_roles=("admin",),
            created_by="alice",
            tables=(ExtractedTable("| a |\n|---|\n| 1 |", 2),),
            images=(ExtractedImage(b"ok", 1), ExtractedImage(b"bad", 3)),
        )
    )

    [document] = indexer.documents
    assert [(a.kind, a.text, a.page) for a in document.assets] == [
        ("table", "| a |\n|---|\n| 1 |", 2),
        ("image", "Image description: caption of ok", 1),
    ]
    assert (result.tables_indexed, result.images_indexed, result.images_skipped) == (1, 1, 1)


@pytest.mark.asyncio
async def test_without_a_captioner_images_are_skipped_not_indexed() -> None:
    indexer = RecordingIndexer()

    result = await IngestionService(FakeRegistry(), indexer).ingest(
        NewDocument("tenant", "acme", "T", "p", ("admin",), "alice", images=(ExtractedImage(b"x"),))
    )

    assert indexer.documents[0].assets == () and result.images_skipped == 1


def test_file_fingerprint_separates_pdfs_with_identical_prose() -> None:
    roles = ("admin",)

    assert content_checksum("same", roles, "file-a") != content_checksum("same", roles, "file-b")
    assert content_checksum("same", roles) == content_checksum(" same ", roles)


@pytest.mark.asyncio
async def test_a_top_table_is_not_pushed_behind_the_prose_budget() -> None:
    from app.generation.prompts import select_context

    text = [
        RetrievedChunk("d", f"t{i}", "Doc", "tenant", "x" * 1_000, 0.6 - i * 0.01)
        for i in range(12)
    ]
    table = RetrievedChunk("d", "a0", "Doc", "tenant", "| table |", 0.9, "table")
    service = RetrievalService(
        CombinedRetriever(FakeRetriever(text), FakeRetriever([table]), 3), 12
    )

    context = select_context(await service.retrieve("q", USER), max_chars=10_000)

    assert "a0" in [c.chunk_id for c in context]  # tied for best of its group, so it ranks first


@pytest.mark.asyncio
async def test_figures_dropped_by_the_cap_are_reported_as_skipped() -> None:
    result = await IngestionService(FakeRegistry(), RecordingIndexer(), FlakyCaptioner()).ingest(
        NewDocument(
            "tenant",
            "acme",
            "T",
            "p",
            ("admin",),
            "alice",
            images=(ExtractedImage(b"ok"), ExtractedImage(b"bad")),
            images_dropped=3,
        )
    )

    assert (result.images_indexed, result.images_skipped) == (1, 4)  # 1 caption failed + 3 dropped


@pytest.mark.asyncio
async def test_figure_text_carries_section_pdf_caption_and_description() -> None:
    indexer = RecordingIndexer()
    image = ExtractedImage(b"ok", 7, caption="Figure 3. Revenue by quarter.", heading="4. Results")

    await IngestionService(FakeRegistry(), indexer, FlakyCaptioner()).ingest(
        NewDocument("tenant", "acme", "T", "p", ("admin",), "alice", images=(image,))
    )

    assert indexer.documents[0].assets[0].text == (
        "Section: 4. Results\nFigure caption: Figure 3. Revenue by quarter.\n"
        "Image description: caption of ok"
    )


@pytest.mark.asyncio
async def test_a_pdf_caption_still_indexes_a_figure_the_vision_model_could_not_describe() -> None:
    indexer = RecordingIndexer()
    captioned = ExtractedImage(b"bad", 2, caption="Figure 1. Map of the site.")
    bare = ExtractedImage(b"bad", 3)

    result = await IngestionService(FakeRegistry(), indexer, FlakyCaptioner()).ingest(
        NewDocument("tenant", "acme", "T", "p", ("admin",), "alice", images=(captioned, bare))
    )

    assert [a.text for a in indexer.documents[0].assets] == [
        "Figure caption: Figure 1. Map of the site."
    ]
    assert (result.images_indexed, result.images_skipped) == (1, 1)


@pytest.mark.asyncio
async def test_table_chunks_carry_their_section_heading() -> None:
    indexer = RecordingIndexer()
    table = ExtractedTable("| a |\n|---|\n| 1 |", 2, heading="2. Budget")

    await IngestionService(FakeRegistry(), indexer).ingest(
        NewDocument("tenant", "acme", "T", "p", ("admin",), "alice", tables=(table,))
    )

    assert indexer.documents[0].assets[0].text == "Section: 2. Budget\n| a |\n|---|\n| 1 |"

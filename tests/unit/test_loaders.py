from pathlib import Path

import pytest

from app.ingestion import loaders
from app.ingestion.loaders import extract_document

REPORT_PDF = Path(__file__).parent.parent / "fixtures" / "report.pdf"


def test_extracts_plain_text_and_strips_bom() -> None:
    assert extract_document("notes.txt", b"\xef\xbb\xbfhello world").text == "hello world"
    document = extract_document("README.MD", b"# Title\nbody")
    assert document.text == "# Title\nbody" and not document.tables and not document.images


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("malware.exe", b"MZ", "supported"),
        ("noext", b"text", "supported"),
        ("latin.txt", "caf\xe9".encode("latin-1"), "UTF-8"),
        ("empty.txt", b"   \n", "no extractable text"),
        ("fake.pdf", b"not a pdf", "not a valid PDF"),
    ],
)
def test_rejects_unusable_input(filename: str, content: bytes, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        extract_document(filename, content)


def test_pdf_converter_failures_become_a_value_error(monkeypatch) -> None:
    class Broken:
        def convert(self, *args, **kwargs):
            raise RuntimeError("encrypted or damaged")

    monkeypatch.setattr(loaders, "_converter", lambda: Broken())

    with pytest.raises(ValueError, match="Could not read the PDF"):
        extract_document("broken.pdf", b"%PDF-1.4\ngarbage")


def test_oversized_text_is_rejected() -> None:
    with pytest.raises(ValueError, match="maximum supported length"):
        extract_document("big.txt", b"a" * (loaders.MAX_EXTRACTED_CHARS + 1))


@pytest.mark.slow
def test_docling_extracts_prose_table_and_image_from_a_pdf() -> None:
    """Runs the real Docling models (tens of seconds): `pytest -m slow`."""
    document = extract_document("report.pdf", REPORT_PDF.read_bytes())

    assert "Regional Planning Report" in document.text
    assert "Headcount" not in document.text.replace("Headcount and budget", "")  # table is apart
    [table] = document.tables
    assert "| Nordics" in table.markdown and "9,800" in table.markdown
    [image] = document.images
    assert image.png.startswith(b"\x89PNG")


def test_logos_and_icons_are_ignored_but_figures_are_kept() -> None:
    from PIL import Image

    class Picture:
        def __init__(self, size: tuple[int, int]) -> None:
            self._image = Image.new("RGB", size, "white")

        def get_image(self, doc):
            return self._image

    assert loaders._picture_png(Picture((230, 68)), None) is None  # wide logo, short side < 100
    assert loaders._picture_png(Picture((44, 23)), None) is None
    assert loaders._picture_png(Picture((480, 422)), None).startswith(b"\x89PNG")


def _table(rows: int) -> str:
    body = "\n".join(f"| row{i:03d} | {i * 7} |" for i in range(rows))
    return f"Table 9. Widgets per site.\n\n| Site | Widgets |\n|------|---------|\n{body}"


def test_small_tables_are_kept_whole() -> None:
    assert loaders.split_table(_table(3), max_chars=2_000) == [_table(3)]


def test_long_tables_split_into_row_groups_that_repeat_caption_and_header() -> None:
    parts = loaders.split_table(_table(120), max_chars=500)

    assert len(parts) > 3
    assert all(len(part) <= 500 + 40 for part in parts)  # one row of slack at most
    for part in parts:
        assert part.startswith("Table 9. Widgets per site.\n| Site | Widgets |\n|------|---------|")
    rows = [line for part in parts for line in part.splitlines() if line.startswith("| row")]
    assert rows == [f"| row{i:03d} | {i * 7} |" for i in range(120)]  # every row, once, in order


def test_a_non_pipe_table_is_bounded_not_dropped() -> None:
    assert len(loaders.split_table("x" * 50_000, max_chars=1_000)) == 1
    assert len(loaders.split_table("x" * 50_000, max_chars=1_000)[0]) == 3_000

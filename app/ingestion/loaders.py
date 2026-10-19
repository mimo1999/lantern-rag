import io
import logging
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf"}
# Docling runs layout and table models on CPU (seconds per page), so the page limit is lower
# than a plain text extractor would need, and the whole conversion has a time budget.
MAX_PDF_PAGES = 100
PDF_TIMEOUT_SECONDS = 300
MAX_EXTRACTED_CHARS = 1_000_000
# ponytail: ~5 s of CPU captioning per image, so this bounds upload time.
MAX_IMAGES_PER_DOCUMENT = 25
# Logos, badges and icons are small; the figures in a paper or report are not.
MIN_IMAGE_SIDE_PX = 100
MAX_IMAGE_SIDE_PX = 1024  # the vision model does not need more, and it is faster with less
# A longer table is split into row groups of about this size, each repeating the caption and
# header so a row is never separated from its column names.
MAX_TABLE_CHUNK_CHARS = 2_000
MAX_TABLE_CHUNKS = 300  # per document; bounds embedding work on a pathological table


@dataclass(frozen=True, slots=True)
class ExtractedTable:
    markdown: str
    page: int | None = None
    heading: str = ""  # nearest section heading, for context


@dataclass(frozen=True, slots=True)
class ExtractedImage:
    png: bytes
    page: int | None = None
    caption: str = ""  # the figure's own caption from the PDF, if Docling found one
    heading: str = ""


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """Prose goes through the text chunker; tables and images are indexed separately."""

    text: str
    tables: tuple[ExtractedTable, ...] = ()
    images: tuple[ExtractedImage, ...] = ()
    images_dropped: int = 0  # real figures beyond MAX_IMAGES_PER_DOCUMENT, not indexed


def split_table(markdown: str, max_chars: int = MAX_TABLE_CHUNK_CHARS) -> list[str]:
    """Split a Markdown table into row groups that each repeat the caption and header."""
    markdown = markdown.strip()
    lines = markdown.splitlines()
    first = next((i for i, line in enumerate(lines) if line.lstrip().startswith("|")), None)
    if first is None:
        return [markdown[: 3 * max_chars]]  # not a pipe table; keep it bounded
    if len(markdown) <= max_chars:
        return [markdown]
    prefix = "\n".join([line for line in lines[:first] if line.strip()] + lines[first : first + 2])
    chunks: list[str] = []
    rows: list[str] = []
    size = len(prefix)
    for row in lines[first + 2 :]:
        if rows and size + len(row) + 1 > max_chars:
            chunks.append(prefix + "\n" + "\n".join(rows))
            rows, size = [], len(prefix)
        rows.append(row)
        size += len(row) + 1
    if rows:
        chunks.append(prefix + "\n" + "\n".join(rows))
    return chunks or [markdown]


def extract_document(filename: str, content: bytes) -> ExtractedDocument:
    """Extract text (and, for PDFs, tables and images) from untrusted uploaded bytes;
    raises ValueError if the file is unusable."""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError("Only .md, .txt and .pdf documents are supported")
    document = (
        _extract_pdf(content) if suffix == ".pdf" else ExtractedDocument(_decode_text(content))
    )
    if len(document.text) > MAX_EXTRACTED_CHARS:
        raise ValueError("Document text exceeds the maximum supported length")
    if not document.text.strip() and not document.tables and not document.images:
        raise ValueError("Document contains no extractable text")
    return document


def _decode_text(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Text documents must be UTF-8 encoded") from error


@lru_cache(maxsize=1)
def _converter():  # type: ignore[no-untyped-def]
    # Imported lazily: loading Docling pulls in PyTorch, which Markdown/text uploads and the
    # rest of the test suite do not need. OCR is off (scanned PDFs are not supported yet).
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(
        do_ocr=False,
        generate_picture_images=True,
        images_scale=2.0,
        document_timeout=PDF_TIMEOUT_SECONDS,
    )
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )


_convert_lock = threading.Lock()  # one CPU-heavy conversion at a time


def _extract_pdf(content: bytes) -> ExtractedDocument:
    if not content.startswith(b"%PDF-"):
        raise ValueError("File is not a valid PDF")
    try:
        from docling.datamodel.base_models import ConversionStatus
        from docling_core.types.doc import (
            PictureItem,
            SectionHeaderItem,
            TableItem,
            TextItem,
        )
        from docling_core.types.io import DocumentStream

        with _convert_lock:
            result = _converter().convert(
                DocumentStream(name="upload.pdf", stream=io.BytesIO(content)),
                max_num_pages=MAX_PDF_PAGES,
            )
        if result.status != ConversionStatus.SUCCESS:
            raise ValueError("Could not read the PDF completely")
        doc = result.document
        paragraphs: list[str] = []
        tables: list[ExtractedTable] = []
        images: list[ExtractedImage] = []
        dropped = 0
        heading = ""
        for item, _ in doc.iterate_items():
            page = item.prov[0].page_no if getattr(item, "prov", None) else None
            if isinstance(item, TableItem):
                markdown = item.export_to_markdown(doc).strip()
                for part in split_table(markdown) if len(markdown) >= 10 else []:
                    if len(tables) < MAX_TABLE_CHUNKS:
                        tables.append(ExtractedTable(part, page, heading))
                    else:
                        logger.warning("table_chunks_over_limit", extra={"page": page})
            elif isinstance(item, PictureItem):
                image = _picture_png(item, doc)
                if image is None:
                    continue
                if len(images) < MAX_IMAGES_PER_DOCUMENT:
                    caption = (item.caption_text(doc) or "").strip()
                    images.append(ExtractedImage(image, page, caption, heading))
                else:
                    dropped += 1
            elif isinstance(item, TextItem) and item.text.strip():
                if isinstance(item, SectionHeaderItem):
                    heading = item.text.strip()
                paragraphs.append(item.text)
    except ValueError:
        raise
    except Exception as error:  # Docling raises many unrelated types on malformed input
        logger.warning("pdf_conversion_failed", extra={"error": type(error).__name__})
        raise ValueError(
            f"Could not read the PDF (it may be damaged, encrypted, over {MAX_PDF_PAGES} pages, "
            "or too slow to process)"
        ) from error
    return ExtractedDocument("\n\n".join(paragraphs), tuple(tables), tuple(images), dropped)


def _picture_png(item, doc) -> bytes | None:  # type: ignore[no-untyped-def]
    image = item.get_image(doc)
    if image is None or min(image.size) < MIN_IMAGE_SIDE_PX:
        return None
    image = image.convert("RGB")
    image.thumbnail((MAX_IMAGE_SIDE_PX, MAX_IMAGE_SIDE_PX))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()

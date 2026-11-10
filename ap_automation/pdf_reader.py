"""Stage 1-2 helpers: check the file is a usable PDF, then get its text or page images.

- Digital PDFs have a text layer -> we read text + tables locally (fast, free, and lets us
  later prove that every value the AI returned really appears in the document).
- Scanned PDFs have no text layer -> we render each page to an image for the vision model.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pdfplumber
import pypdfium2 as pdfium

MAX_FILE_MB = 10
MAX_PAGES = 5
MIN_TEXT_CHARS = 40


class InvalidDocument(Exception):
    """The upload is not something we can process (empty, not a PDF, damaged...)."""


@dataclass
class DocumentContent:
    page_count: int
    text: str = ""
    tables: list = field(default_factory=list)
    has_text_layer: bool = False
    page_images: list[bytes] = field(default_factory=list)


def inspect_pdf(file_bytes: bytes) -> int:
    """Return the page count or raise InvalidDocument with a human-readable reason."""
    if not file_bytes:
        raise InvalidDocument("The file is empty.")
    if len(file_bytes) > MAX_FILE_MB * 1024 * 1024:
        raise InvalidDocument(f"The file is larger than {MAX_FILE_MB} MB.")
    if not file_bytes.lstrip()[:5] == b"%PDF-":
        raise InvalidDocument("This is not a PDF file (the PDF header is missing).")
    try:
        document = pdfium.PdfDocument(file_bytes)
        pages = len(document)
        document.close()
    except Exception as exc:  # pdfium raises PdfiumError for damaged files
        raise InvalidDocument(f"The PDF is damaged and cannot be opened ({exc.__class__.__name__}).") from exc
    if pages == 0:
        raise InvalidDocument("The PDF has no pages.")
    return pages


def render_page_png(file_bytes: bytes, page_index: int = 0, scale: float = 2.0) -> bytes:
    document = pdfium.PdfDocument(file_bytes)
    try:
        image = document[page_index].render(scale=scale).to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    finally:
        document.close()


def read_document(file_bytes: bytes) -> DocumentContent:
    page_count = inspect_pdf(file_bytes)
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        pages = pdf.pages[:MAX_PAGES]
        text = "\n".join((page.extract_text() or "") for page in pages).strip()
        tables = [table for page in pages for table in (page.extract_tables() or [])]
    has_text_layer = len(re.sub(r"\s", "", text)) >= MIN_TEXT_CHARS
    content = DocumentContent(page_count=page_count, text=text, tables=tables, has_text_layer=has_text_layer)
    if not has_text_layer:
        content.page_images = [render_page_png(file_bytes, i) for i in range(min(page_count, MAX_PAGES))]
    return content

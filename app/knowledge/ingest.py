"""
Guideline document ingestion: PDF -> page-tracked text -> chunks with
citation metadata, ready to embed and store in the vector index.

Chunking strategy (recursive, paragraph-first):
  1. Split each page into paragraphs on blank lines.
  2. Greedily pack paragraphs into a chunk up to `max_chars`.
  3. Start the next chunk with the tail of the previous one (`overlap_chars`,
     snapped to a sentence boundary where possible) so a rule that spans a
     chunk boundary isn't lost from either chunk's context.
  4. A single paragraph longer than `max_chars` (dense tables, long
     enumerated lists -- common in agency guides) is split on sentence
     boundaries rather than dropped or truncated.

Every chunk keeps its source page range, because the whole point of
citing a guideline answer is being able to tell the borrower/loan officer
*where in the guide* it came from.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import pdfplumber


@dataclass
class PageText:
    page_number: int
    text: str


@dataclass
class GuidelineChunk:
    chunk_id: str
    text: str
    source: str
    page_start: int
    page_end: int
    loan_types: list[str]


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


def extract_pages(file_bytes: bytes) -> list[PageText]:
    pages = []
    with pdfplumber.open(__import__("io").BytesIO(file_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(PageText(page_number=i + 1, text=text))
    return pages


def _split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    sentences = _SENTENCE_SPLIT.split(paragraph)
    pieces, current = [], ""
    for sentence in sentences:
        if current and len(current) + len(sentence) + 1 > max_chars:
            pieces.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current.strip())
    return pieces


def _sentence_boundary_tail(text: str, target_len: int) -> str:
    """Take roughly the last `target_len` chars of `text`, snapped
    forward to the start of a sentence so overlap reads naturally."""
    if len(text) <= target_len:
        return text
    tail = text[-target_len:]
    m = re.search(r"[.!?]\s+[A-Z(]", tail)
    return tail[m.end() - 1:] if m else tail


def chunk_pages(
    pages: list[PageText],
    source: str,
    loan_types: list[str],
    max_chars: int = 1200,
    overlap_chars: int = 200,
) -> list[GuidelineChunk]:
    # Flatten to (page_number, paragraph) pairs, splitting on blank lines
    # and also breaking any single paragraph that's already too long.
    units: list[tuple[int, str]] = []
    for page in pages:
        for para in re.split(r"\n\s*\n", page.text):
            para = para.strip()
            if not para:
                continue
            if len(para) > max_chars:
                units.extend((page.page_number, p) for p in _split_long_paragraph(para, max_chars))
            else:
                units.append((page.page_number, para))

    chunks: list[GuidelineChunk] = []
    current_text = ""
    current_pages: set[int] = set()
    chunk_idx = 0

    def _flush():
        nonlocal current_text, current_pages, chunk_idx
        if not current_text.strip():
            return
        chunks.append(GuidelineChunk(
            chunk_id=f"{source}::chunk_{chunk_idx}",
            text=current_text.strip(),
            source=source,
            page_start=min(current_pages),
            page_end=max(current_pages),
            loan_types=loan_types,
        ))
        chunk_idx += 1

    for page_number, para in units:
        candidate = f"{current_text}\n\n{para}".strip() if current_text else para
        if len(candidate) > max_chars and current_text:
            _flush()
            tail = _sentence_boundary_tail(current_text, overlap_chars)
            current_text = f"{tail}\n\n{para}".strip()
            current_pages = {page_number}
            # keep the page(s) the overlap tail came from too, best-effort:
            if chunks:
                current_pages.add(chunks[-1].page_end)
        else:
            current_text = candidate
            current_pages.add(page_number)

    _flush()
    return chunks


def ingest_pdf(file_bytes: bytes, source: str, loan_types: list[str]) -> list[GuidelineChunk]:
    pages = extract_pages(file_bytes)
    if not pages:
        raise ValueError(f"No extractable text found in '{source}' -- is it a scanned/image PDF? "
                          f"Run it through OCR first (see app/documents/ocr.py) before ingesting.")
    return chunk_pages(pages, source=source, loan_types=loan_types)

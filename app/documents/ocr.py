"""
OCR extraction layer.

Strategy per file:
 1. PDF with a real text layer (most paystubs/bank statements exported
    from payroll/banking software) -> pdfplumber, no OCR needed, highest
    accuracy, fastest, and free.
 2. PDF that's actually a scan (no/negligible text layer), or a plain
    image (jpg/png of a photographed document) -> rasterize with
    pdf2image and run Tesseract, with OpenCV preprocessing first
    (grayscale, denoise, adaptive threshold, deskew) since raw phone
    photos of paystubs are what most borrowers actually upload.

Returns page-level text plus a rough per-page OCR confidence (Tesseract
exposes per-word confidence; we average it) so the parser layer can
decide whether a document needs human review purely from image quality,
independent of whether expected fields were found.

Swap-out point for production: replace `_ocr_image` with a call to a
cloud Document AI / specialized form-parsing model once volume justifies
it -- generic OCR + regex (what this module does) works but will need a
larger human-review queue than a trained parser would.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

import cv2
import numpy as np
import pdfplumber
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image

logger = logging.getLogger(__name__)

# Below this many characters per page, a PDF's "text layer" is treated as
# absent/unreliable (e.g. a scanned page with a stray OCR text layer from
# a previous pass, or a mostly-blank cover page) and we fall back to OCR.
MIN_CHARS_FOR_NATIVE_TEXT = 40


@dataclass
class PageResult:
    page_number: int
    text: str
    method: str  # "native_text" | "ocr"
    ocr_confidence: float | None = None  # None when method == native_text


@dataclass
class DocumentText:
    pages: list[PageResult] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)

    @property
    def overall_confidence(self) -> float:
        """
        1.0 for any page read from a native text layer (no OCR error to
        speak of); OCR pages contribute their measured confidence.
        Whole-document confidence is the average across pages, weighted
        toward the worst page so one bad scan can't be hidden by three
        good ones.
        """
        if not self.pages:
            return 0.0
        scores = [1.0 if p.method == "native_text" else (p.ocr_confidence or 0.0) for p in self.pages]
        return round(0.5 * (sum(scores) / len(scores)) + 0.5 * min(scores), 3)


def _preprocess_for_ocr(pil_image: Image.Image) -> Image.Image:
    """Grayscale + denoise + adaptive threshold + deskew. Meaningfully
    improves Tesseract accuracy on phone-camera photos of paystubs,
    which is the realistic upload path for most borrowers."""
    img = cv2.cvtColor(np.array(pil_image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    img = cv2.fastNlMeansDenoising(img, h=10)
    img = cv2.adaptiveThreshold(
        img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
    )

    coords = cv2.findNonZero(255 - img)
    if coords is not None:
        angle = cv2.minAreaRect(coords)[-1]
        angle = -(90 + angle) if angle < -45 else -angle
        if abs(angle) > 0.5:  # only correct meaningful skew; avoid noise on clean scans
            (h, w) = img.shape
            m = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
            img = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

    return Image.fromarray(img)


def _ocr_image(pil_image: Image.Image) -> tuple[str, float]:
    processed = _preprocess_for_ocr(pil_image)
    data = pytesseract.image_to_data(processed, output_type=pytesseract.Output.DICT, lang="eng")

    words, confidences = [], []
    for text, conf in zip(data["text"], data["conf"]):
        text = text.strip()
        conf = float(conf)
        if text and conf >= 0:
            words.append(text)
            confidences.append(conf)

    full_text = " ".join(words)
    avg_conf = (sum(confidences) / len(confidences) / 100.0) if confidences else 0.0
    return full_text, avg_conf


def extract_pdf(file_bytes: bytes) -> DocumentText:
    doc = DocumentText()

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        native_pages: dict[int, str] = {}
        pages_needing_ocr: list[int] = []

        for i, page in enumerate(pdf.pages):
            text = (page.extract_text() or "").strip()
            if len(text) >= MIN_CHARS_FOR_NATIVE_TEXT:
                native_pages[i] = text
            else:
                pages_needing_ocr.append(i)

        if pages_needing_ocr:
            images = convert_from_bytes(file_bytes, dpi=300)
            for i in pages_needing_ocr:
                text, conf = _ocr_image(images[i])
                doc.pages.append(PageResult(page_number=i + 1, text=text, method="ocr", ocr_confidence=conf))

        for i, text in native_pages.items():
            doc.pages.append(PageResult(page_number=i + 1, text=text, method="native_text"))

    doc.pages.sort(key=lambda p: p.page_number)
    return doc


def extract_image(file_bytes: bytes) -> DocumentText:
    pil_image = Image.open(io.BytesIO(file_bytes))
    text, conf = _ocr_image(pil_image)
    return DocumentText(pages=[PageResult(page_number=1, text=text, method="ocr", ocr_confidence=conf)])


def extract(file_bytes: bytes, filename: str) -> DocumentText:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return extract_pdf(file_bytes)
    if lower.endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp")):
        return extract_image(file_bytes)
    raise ValueError(f"Unsupported file type for OCR: {filename}")

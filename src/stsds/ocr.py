"""Node 2: text extraction. Embedded text layer first, PaddleOCR only for scanned pages."""

from __future__ import annotations

import io
from typing import Protocol

from pypdf import PdfReader

from .gates import GateFailure

MIN_CHARS_PER_PAGE = 120


class TextExtractor(Protocol):
    def extract(self, pdf: bytes) -> str: ...


class PdfTextExtractor:
    def extract(self, pdf: bytes) -> str:
        reader = PdfReader(io.BytesIO(pdf))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        if len(text.strip()) >= MIN_CHARS_PER_PAGE * max(len(reader.pages), 1):
            return text
        return self._ocr(pdf)

    @staticmethod
    def _ocr(pdf: bytes) -> str:
        try:  # heavy optional dependencies, installed only in the OCR worker image
            import numpy as np
            from paddleocr import PaddleOCR
            from pdf2image import convert_from_bytes
        except ImportError as exc:
            raise GateFailure(
                "OCR_UNAVAILABLE", "Scanned document and the OCR engine is not installed", "HALT"
            ) from exc
        engine = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
        lines: list[str] = []
        for image in convert_from_bytes(pdf, dpi=300):
            result = engine.ocr(np.array(image), cls=True) or []
            for page in result:
                lines.extend(str(item[1][0]) for item in (page or []))
        text = "\n".join(lines)
        if not text.strip():
            raise GateFailure("OCR_EMPTY", "OCR produced no text", "REVIEW")
        return text

"""Reading and checking the QR code printed on the Sijil Setem (stamp certificate).

Two separate jobs, deliberately kept apart:

  1. ``read_qr_urls`` turns the certificate PDF into the text inside its QR codes. It is a pure
     function with no network access, so it is safe to run on the one-time download immediately.
  2. ``check_qr_url`` decides whether a decoded URL may be opened at all. A QR code is attacker-
     controllable input (a forged certificate can point anywhere), so the URL must be https, carry
     no credentials, use the default port, and sit on a host the operator has allow-listed. Anything
     else fails closed; the portal is never asked to open it.

Whether the page behind the URL says "valid" is the portal's answer, read by the MyTax port's
``verify_qr`` (see rpa.py). This module never decides validity itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit

from .gates import GateFailure

MAX_URL_LEN = 2048
MAX_PAGES = 4  # a certificate is short; bounding pages bounds the work a hostile PDF can cause
RENDER_SCALES = (2.0, 3.0, 4.0)  # retry larger if a small code is not found


@dataclass(frozen=True)
class QrVerification:
    """What the portal's verification page said. ``valid`` is None when the page cannot be read."""

    valid: bool | None
    certificate_ref: str | None = None
    amount_rm: Decimal | None = None
    detail: str = ""


def read_qr_urls(pdf: bytes) -> list[str]:
    """Return every distinct QR payload found on the first pages of ``pdf`` (may be empty)."""
    import cv2
    import numpy as np
    import pypdfium2 as pdfium

    found: list[str] = []
    document = pdfium.PdfDocument(pdf)
    try:
        detector = cv2.QRCodeDetector()
        for index in range(min(len(document), MAX_PAGES)):
            page = document[index]
            for scale in RENDER_SCALES:
                bitmap = page.render(scale=scale)
                image = np.ascontiguousarray(bitmap.to_numpy())
                gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY if image.shape[-1] == 3 else cv2.COLOR_RGBA2GRAY)
                texts: list[str] = []
                try:
                    ok, decoded, _points, _ = detector.detectAndDecodeMulti(gray)
                    if ok:
                        texts = [t for t in decoded if t]
                except cv2.error:
                    texts = []
                if not texts:
                    single, _pts, _ = detector.detectAndDecode(gray)
                    texts = [single] if single else []
                for text in texts:
                    if text not in found:
                        found.append(text)
                if texts:
                    break  # this page is done
    finally:
        document.close()
    return found


def check_qr_url(url: str, allowed_hosts: frozenset[str]) -> str:
    """Return the host if ``url`` is safe to open, else raise. Fails closed on an empty allow-list."""
    if not allowed_hosts:
        raise GateFailure("QR_UNCONFIGURED", "No QR verification host is allow-listed", "HALT")
    if len(url) > MAX_URL_LEN or any(ch in url for ch in "\r\n\t "):
        raise GateFailure("QR_URL_REJECTED", "QR code does not hold a plain URL", "HALT")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        raise GateFailure("QR_URL_REJECTED", "QR URL must be plain https on the default port", "HALT")
    if host not in allowed_hosts:
        raise GateFailure("QR_HOST_NOT_ALLOWED", f"QR points to a host that is not allow-listed: {host[:80]}", "HALT")
    return host


def parse_hosts(value: str | None) -> frozenset[str]:
    return frozenset(h.strip().lower() for h in (value or "").split(",") if h.strip())

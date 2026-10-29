"""Step 1 of the workflow: get the document signed, then hand the signed PDF to the pipeline.

How it fits:

  request()  ->  provider sends the document to the signers, we remember the envelope
  collect()  ->  we ask the provider (never trust a callback body) whether everyone has signed;
                 once they have, the signed PDF is ingested like any other document
  webhook    ->  the provider's callback only says "look at envelope X"; collect() does the rest

Honest limit: ``signature_report`` checks that signature fields exist and that each one covers the
whole file (structure). It does not prove a certificate chain. Cryptographic validation belongs to
the signing provider (and can be added with a validator such as pyHanko behind ``SignatureVerifier``).
The structural check still catches the common failures: unsigned files, and files edited after
signing, because an edit appended after signing leaves the signed byte range short of the file.

Which signing service is used (MSC Trustgate, a corporate e-sign platform, etc.) is a deployment
decision. ``HttpSignatureProvider`` speaks a small JSON contract a thin gateway can implement.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .gates import GateFailure, check_pdf
from .security import mask

SignState = Literal["PENDING", "COMPLETED", "DECLINED", "EXPIRED"]
EMAIL_RE = re.compile(r"^[^@\s,;<>]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,}$")
MAX_SIGNERS = 10
ENVELOPE_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


@dataclass(frozen=True)
class Signer:
    name: str
    email: str

    def validate(self) -> None:
        if not (1 <= len(self.name.strip()) <= 120) or not self.name.isprintable():
            raise GateFailure("SIGNER_INVALID", "Signer name is missing or too long", "HALT")
        if not EMAIL_RE.match(self.email):
            raise GateFailure("SIGNER_INVALID", f"Signer email is not valid: {mask(self.email)}", "HALT")


@dataclass(frozen=True)
class SigningStatus:
    state: SignState
    signed_pdf: bytes | None = None
    signers_done: int = 0
    signers_total: int = 0


class SignatureProvider(Protocol):
    async def request(self, *, pdf: bytes, signers: list[Signer], reference: str) -> str: ...
    async def status(self, envelope_id: str) -> SigningStatus: ...


@dataclass(frozen=True)
class SignatureReport:
    total: int  # signature fields that hold a signature value
    covering: int  # of those, how many cover the whole file


def signature_report(pdf: bytes) -> SignatureReport:
    """Count signature fields and how many of them cover the entire file (structural check only)."""
    try:
        fields = PdfReader(io.BytesIO(pdf)).get_fields() or {}
    except PyPdfError:
        return SignatureReport(0, 0)
    total = covering = 0
    for fld in fields.values():
        if fld.get("/FT") != "/Sig":
            continue
        value = fld.get("/V")
        if value is None:
            continue
        value = value.get_object() if hasattr(value, "get_object") else value
        contents = value.get("/Contents")
        byte_range = value.get("/ByteRange")
        if not contents:
            continue
        total += 1
        try:
            r = [int(x) for x in byte_range]
        except (TypeError, ValueError):
            continue
        # [0, a, b, c]: the file is signed up to a, skips the signature hex [a, b), signs b..b+c.
        if len(r) == 4 and r[0] == 0 and r[2] + r[3] >= len(pdf.rstrip(b"\r\n \t")):
            covering += 1
    return SignatureReport(total, covering)


def signature_gate(pdf: bytes, minimum: int) -> SignatureReport:
    """Raise unless the PDF carries at least ``minimum`` whole-file signatures. 0 disables the gate."""
    report = signature_report(pdf)
    if minimum <= 0:
        return report
    if report.total < minimum:
        raise GateFailure(
            "SIGNATURE_MISSING", f"Document has {report.total} signature(s); {minimum} required", "REVIEW"
        )
    if report.covering < minimum:
        raise GateFailure(
            "SIGNATURE_BROKEN", "A signature does not cover the whole file; it may have been edited after signing", "REVIEW"
        )
    return report


@dataclass
class SigningRecord:
    envelope_id: str
    reference: str
    name: str
    requested_by: str
    signers: list[str]  # names only; emails stay with the provider
    state: str = "PENDING"
    txn_id: str | None = None
    created: float = field(default_factory=time.time)
    signers_done: int = 0

    def public(self) -> dict[str, Any]:
        return {
            "envelope_id": self.envelope_id, "name": self.name, "state": self.state, "txn_id": self.txn_id,
            "signers": self.signers, "signers_done": self.signers_done, "requested_by": self.requested_by,
        }


class SigningService:
    """Persists one small JSON file per envelope and moves signed documents into the pipeline."""

    def __init__(self, root: str | Path, provider: SignatureProvider, ingest: Any, notifier: Any) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._provider, self._ingest, self._notifier = provider, ingest, notifier
        self._locks: dict[str, asyncio.Lock] = {}

    def _path(self, envelope_id: str) -> Path:
        if not ENVELOPE_RE.match(envelope_id):
            raise GateFailure("ENVELOPE_INVALID", "Invalid envelope id", "HALT")
        return self._root / f"{envelope_id}.json"

    def _save(self, rec: SigningRecord) -> None:
        fd, tmp = tempfile.mkstemp(dir=self._root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(rec.__dict__, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path(rec.envelope_id))
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise

    def get(self, envelope_id: str) -> SigningRecord:
        try:
            return SigningRecord(**json.loads(self._path(envelope_id).read_text()))
        except (OSError, ValueError, TypeError) as exc:
            raise GateFailure("ENVELOPE_UNKNOWN", "Unknown signing envelope", "HALT") from exc

    async def request(self, pdf: bytes, name: str, signers: list[Signer], requested_by: str) -> SigningRecord:
        check_pdf(pdf)
        if not 1 <= len(signers) <= MAX_SIGNERS:
            raise GateFailure("SIGNER_INVALID", f"Between 1 and {MAX_SIGNERS} signers are required", "HALT")
        for s in signers:
            s.validate()
        clean = "".join(ch for ch in name if ch.isprintable())[:80]
        reference = f"stsds-{int(time.time())}"
        envelope_id = await self._provider.request(pdf=pdf, signers=signers, reference=reference)
        rec = SigningRecord(envelope_id, reference, clean, requested_by, [s.name for s in signers])
        self._path(envelope_id)  # validates the provider-issued id before we trust it as a filename
        self._save(rec)
        return rec

    async def collect(self, envelope_id: str) -> tuple[SigningRecord, str | None]:
        """Ask the provider for the truth. Returns the record and, when newly ingested, the txn id."""
        lock = self._locks.setdefault(envelope_id, asyncio.Lock())
        async with lock:
            rec = self.get(envelope_id)
            if rec.txn_id:  # already handed over: idempotent
                return rec, None
            status = await self._provider.status(envelope_id)
            rec.state, rec.signers_done = status.state, status.signers_done
            if status.state in {"DECLINED", "EXPIRED"}:
                self._save(rec)
                self._notifier.alert(envelope_id, f"SIGNING_{status.state}", f"Signing {status.state.lower()} for {rec.name}", "REVIEW")
                return rec, None
            if status.state != "COMPLETED" or not status.signed_pdf:
                self._save(rec)
                return rec, None
            result = self._ingest(status.signed_pdf, f"esign:{envelope_id}", name=rec.name)
            rec.txn_id = result.txn_id
            self._save(rec)
            return rec, result.txn_id


class HttpSignatureProvider:
    """JSON contract (see README): POST /envelopes, GET /envelopes/{id}. Bearer auth, TLS only."""

    def __init__(self, base_url: str | None, token: str | None, timeout: float = 30.0) -> None:
        self._url = (base_url or "").rstrip("/")
        self._token, self._timeout = token, timeout
        if self._url and not self._url.startswith("https://"):
            raise ValueError("signing provider URL must be https")

    def _headers(self) -> dict[str, str]:
        if not self._url or not self._token:
            raise GateFailure("SIGNING_UNCONFIGURED", "No signing provider is configured", "HALT")
        return {"Authorization": f"Bearer {self._token}"}

    async def request(self, *, pdf: bytes, signers: list[Signer], reference: str) -> str:
        body = {
            "reference": reference, "pdf_base64": base64.b64encode(pdf).decode(),
            "signers": [{"name": s.name, "email": s.email} for s in signers],
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(f"{self._url}/envelopes", json=body, headers=self._headers())
            resp.raise_for_status()
            return str(resp.json()["envelope_id"])
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise GateFailure("SIGNING_REQUEST_FAILED", f"Could not start signing: {type(exc).__name__}", "HALT") from exc

    async def status(self, envelope_id: str) -> SigningStatus:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{self._url}/envelopes/{envelope_id}", headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
            state = str(data["status"])
            if state not in {"PENDING", "COMPLETED", "DECLINED", "EXPIRED"}:
                raise ValueError("unknown status")
            signed = base64.b64decode(data["signed_pdf_base64"], validate=True) if data.get("signed_pdf_base64") else None
            return SigningStatus(state, signed, int(data.get("signers_done", 0)), int(data.get("signers_total", 0)))  # type: ignore[arg-type]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise GateFailure("SIGNING_STATUS_ERROR", f"Could not read signing status: {type(exc).__name__}", "HALT") from exc

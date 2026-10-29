"""Section 35B vault: one-time Sijil Setem capture, SHA-256, PDF merge, S3 Object Lock (7 years).

Failure drill requirement: the raw Sijil buffer is fsynced to local disk BEFORE any upload is
attempted, because MyTax allows only one download. The local copy is removed only after S3 confirms
COMPLIANCE-mode retention on the stored object.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from .gates import GateFailure
from .schemas import CalcResult

RETENTION_YEARS = 7


class S3Like(Protocol):
    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...
    def head_object(self, **kwargs: Any) -> dict[str, Any]: ...


def add_years(moment: dt.datetime, years: int) -> dt.datetime:
    try:
        return moment.replace(year=moment.year + years)
    except ValueError:  # 29 February
        return moment.replace(year=moment.year + years, day=28)


def render_working_sheet(txn_id: str, calc: CalcResult, lhdn_total: str) -> bytes:
    """Statutory Calculation Working Sheet (Section 3A(2A) audit paper)."""
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    _, height = A4
    y = height - 50
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(40, y, "Stamp Duty Calculation Working Sheet")
    pdf.setFont("Helvetica", 9)
    for line in (
        f"Transaction: {txn_id}",
        f"Instrument: {calc.item.value}   Exempt: {calc.exempt}",
        f"Rules: {calc.rules_version}  sha256 {calc.rules_sha256[:16]}...  Engine {calc.engine_version}",
    ):
        y -= 16
        pdf.drawString(40, y, line)
    y -= 10
    for step in calc.steps:
        y -= 15
        if y < 80:
            pdf.showPage()
            y = height - 50
        pdf.drawString(40, y, f"{step.label}: {step.formula} -> RM{step.amount_rm}"[:120])
    y -= 24
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(40, y, f"Engine total duty: RM{calc.total_duty_rm}")
    y -= 15
    pdf.drawString(40, y, f"LHDN Rumusan Pengiraan: RM{lhdn_total}")
    pdf.save()
    return buf.getvalue()


def merge_pdfs(*documents: bytes) -> bytes:
    writer = PdfWriter()
    for doc in documents:
        for page in PdfReader(io.BytesIO(doc)).pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@dataclass(frozen=True)
class VaultReceipt:
    txn_id: str
    bundle_key: str
    bundle_sha256: str
    sijil_sha256: str
    retain_until: str
    version_id: str | None


class Vault:
    def __init__(
        self,
        s3: S3Like,
        bucket: str,
        kms_key_id: str | None,
        cache_dir: Path,
        clock: Any = None,
    ) -> None:
        self._s3, self._bucket, self._kms = s3, bucket, kms_key_id
        self._cache = cache_dir
        self._cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._now = clock or (lambda: dt.datetime.now(dt.UTC))

    def cache_sijil(self, txn_id: str, buffer: bytes) -> Path:
        """Durably persist the one-time download before doing anything else with it."""
        path = self._cache / f"{txn_id}.sijil.pdf"
        fd, tmp = tempfile.mkstemp(dir=self._cache)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(buffer)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def archive(
        self,
        txn_id: str,
        contract_pdf: bytes,
        bnds_record: dict[str, Any],
        calc: CalcResult,
        lhdn_total: str,
        sijil: bytes,
    ) -> VaultReceipt:
        cached = self.cache_sijil(txn_id, sijil)  # step 1: survive any later failure
        if not sijil.startswith(b"%PDF-"):
            raise GateFailure("SIJIL_INVALID", "Sijil Setem is not a PDF", "HALT")
        sijil_sha = hashlib.sha256(sijil).hexdigest()
        working = render_working_sheet(txn_id, calc, lhdn_total)
        bundle = merge_pdfs(contract_pdf, working, sijil)
        bundle_sha = hashlib.sha256(bundle).hexdigest()
        retain_until = add_years(self._now(), RETENTION_YEARS)
        base = f"stsds/{txn_id}"
        bnds_json = json.dumps(bnds_record, sort_keys=True, default=str).encode()
        put_args: dict[str, Any] = {
            "Bucket": self._bucket,
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": retain_until,
            "ChecksumAlgorithm": "SHA256",
        }
        if self._kms:
            put_args.update(ServerSideEncryption="aws:kms", SSEKMSKeyId=self._kms)
        resp = self._s3.put_object(
            Key=f"{base}/bundle.pdf", Body=bundle, ContentType="application/pdf",
            Metadata={"sha256": bundle_sha, "sijil-sha256": sijil_sha}, **put_args,
        )
        self._s3.put_object(
            Key=f"{base}/bnds.json", Body=bnds_json, ContentType="application/json", **put_args
        )
        self._s3.put_object(
            Key=f"{base}/working-sheet.pdf", Body=working, ContentType="application/pdf", **put_args
        )
        head = self._s3.head_object(Bucket=self._bucket, Key=f"{base}/bundle.pdf")
        if head.get("ObjectLockMode") != "COMPLIANCE":
            raise GateFailure("VAULT_LOCK", "Stored object is not under COMPLIANCE retention", "HALT")
        cached.unlink(missing_ok=True)  # only after the lock is confirmed
        return VaultReceipt(
            txn_id=txn_id,
            bundle_key=f"{base}/bundle.pdf",
            bundle_sha256=bundle_sha,
            sijil_sha256=sijil_sha,
            retain_until=retain_until.isoformat(),
            version_id=resp.get("VersionId"),
        )

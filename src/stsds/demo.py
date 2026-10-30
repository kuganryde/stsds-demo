"""Dry-run harness. NOT FOR PRODUCTION.

Runs the REAL pipeline (gates, engine, parity, maker-checker, audit chain, vault logic) against:
  * a simulated MyTax portal that computes its own total independently of the engine,
  * a local-folder "vault" (no real WORM lock),
  * a heuristic classifier for plain sample contracts (no LLM needed),
  * a permissive TIN check (format only).
It exists so you can watch every gate fire without the portal, an LLM, AWS or n8n.

  python -m stsds.demo                 run six scenarios end to end
  python -m stsds.demo serve           start the real HTTP API with the simulated adapters
  python -m stsds.demo batch           six mixed documents: auto-processing, review and approval
  python -m stsds.demo send F.pdf ...  submit PDFs as a batch to a running 'serve'
  python -m stsds.demo sample          write sample contracts into ./demo-samples
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import json
import math
import re
import secrets
import sys
import tempfile
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .audit import MemoryAuditStore, verify_chain
from .batch import BatchService
from .clients import ClientDirectory
from .gates import GateFailure
from .ledger import Ledger
from .notify import NullNotifier
from .ocr import PdfTextExtractor
from .orchestrator import Orchestrator
from .payment import BillStatus, PaymentResult
from .pipeline import Pipeline, StagingStore
from .qr import QrVerification
from .rpa import DraftResult, PortalBill, SubmitResult
from .rules import Signoff, load_rules
from .schemas import Classification, Party
from .settings import Settings
from .signing import Signer, SigningStatus
from .vault import Vault

BANNER = "DEMO MODE: simulated portal, simulated vault, heuristic classifier. Not for production."
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
DEMO_PARTIES = [
    Party(role="party A", name="Demo Alpha Sdn Bhd", tin="C1234567890", id_type="BRN", id_number="202001012345"),
    Party(role="party B", name="Demo Beta Sdn Bhd", tin="C9876543210", id_type="BRN", id_number="201901054321"),
]
# A second pair of parties, used by the "newclient" sample: neither is registered in the demo client master.
NEW_CLIENT_PARTIES = [
    Party(role="party A", name="Demo Omega Sdn Bhd", tin="C5555555555", id_type="BRN", id_number="202201098765"),
    Party(role="party B", name="Demo Delta Sdn Bhd", tin="C7777777777", id_type="BRN", id_number="201801077777"),
]


# ---------------------------------------------------------------------------------------------
# Sample documents
# ---------------------------------------------------------------------------------------------
def _date_text(d: dt.date) -> str:
    return f"{d.day} {MONTHS[d.month - 1].capitalize()} {d.year}"


_BOILERPLATE = (
    "\nGeneral terms: this agreement is governed by the laws of Malaysia. The parties confirm that they have read"
    "\nand understood its terms, that notices must be given in writing, and that any amendment must be agreed in"
    "\nwriting and signed by both parties. Each party bears its own costs unless stated otherwise in this document."
)


def sample_text(kind: str, signed_on: dt.date) -> str:
    return _sample_core(kind, signed_on) + _BOILERPLATE


def _sample_core(kind: str, signed_on: dt.date) -> str:
    when = _date_text(signed_on)
    if kind == "lease":
        return (f"TENANCY AGREEMENT\nThis agreement is made on {when} between Demo Alpha Sdn Bhd (Landlord) and "
                "Demo Beta Sdn Bhd (Tenant).\nThe Tenant shall pay monthly rent of RM1,500.00 for a term of 12 months.")
    if kind == "newclient":  # a tenancy for a company that has not been onboarded yet
        return (f"TENANCY AGREEMENT\nThis agreement is made on {when} between Demo Omega Sdn Bhd (Landlord) and "
                "Demo Delta Sdn Bhd (Tenant).\nThe Tenant shall pay monthly rent of RM1,200.00 for a term of 12 months.")
    if kind == "blurry":  # same content, but flagged as a poor scan so the demo classifier is unsure
        return (f"TENANCY AGREEMENT (poor scan, illegible stamp)\nThis agreement is made on {when} between Demo Alpha "
                "Sdn Bhd (Landlord) and Demo Beta Sdn Bhd (Tenant).\nThe Tenant shall pay monthly rent of RM2,000.00 "
                "for a term of 24 months.")
    if kind == "loan":
        return (f"FACILITY AGREEMENT dated {when}.\nPrincipal sum RM5,000,000.00 secured by a first legal charge "
                "(secured loan).")
    if kind == "employment":
        return (f"EMPLOYMENT CONTRACT made on {when}.\nThe Employee shall receive a gross monthly salary of RM2,800.00.")
    if kind == "transfer":
        return f"MEMORANDUM OF TRANSFER dated {when}.\nThe consideration for the transfer is RM500,000.00."
    owner = "\nContract owner: Aisha Rahman."
    if kind == "service":
        return (f"SERVICE AGREEMENT dated {when} between Demo Alpha Sdn Bhd and Demo Beta Sdn Bhd.\n"
                f"Contract sum RM120,000.00 for a fixed term of 24 months.{owner}")
    if kind == "shareholders":
        return (f"SHAREHOLDERS AGREEMENT made on {when} between Demo Alpha Sdn Bhd and Demo Beta Sdn Bhd "
                f"governing their holdings in Demo Gamma Sdn Bhd.{owner}")
    if kind == "intercompany":
        return (f"INTERCOMPANY AGREEMENT dated {when} between Demo Alpha Sdn Bhd and Demo Beta Sdn Bhd "
                f"for shared services.{owner}")
    if kind == "shares":
        return (f"SHARE TRANSFER FORM dated {when}.\nThe consideration is RM250,000.00. "
                f"Net tangible assets of the shares transferred: RM300,000.00.{owner}")
    if kind == "loanu":
        return (f"UNSECURED LOAN AGREEMENT dated {when}.\nPrincipal sum RM200,000.00 repayable over a "
                f"fixed term of 24 months.{owner}")
    raise ValueError(kind)


def make_pdf(text: str) -> bytes:
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in text.splitlines():
        pdf.drawString(40, y, line[:110])
        y -= 16
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


def make_certificate(text: str, url: str | None) -> bytes:
    """A simulated Sijil Setem: text plus (optionally) a QR code that holds ``url``."""
    import io

    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode import qr
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in text.splitlines():
        pdf.drawString(40, y, line[:110])
        y -= 16
    if url:
        widget = qr.QrCodeWidget(url)
        x0, y0, x1, y1 = widget.getBounds()
        size = 160
        drawing = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
        drawing.add(widget)
        renderPDF.draw(drawing, pdf, 40, 560)
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


def make_signed_pdf(text: str, signatures: int = 1) -> bytes:
    """A structurally signed PDF (signature fields with a ByteRange covering the file).

    The signature bytes are zeros: this is for exercising the intake gate, not real cryptography.
    """
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    lines = "".join(f"({esc(line[:100])}) Tj 0 -14 Td " for line in text.splitlines())
    stream = f"BT /F1 10 Tf 40 800 Td {lines}ET".encode("latin-1", "replace")
    n = max(1, signatures)
    sig_objs = [10 + 2 * i for i in range(n)]  # field object numbers; value objects follow each
    fields = " ".join(f"{o} 0 R" for o in sig_objs)

    def build(ranges: list[tuple[int, int, int]] | None) -> tuple[bytes, list[tuple[int, int]]]:
        out = bytearray(b"%PDF-1.7\n")
        offsets: dict[int, int] = {}
        holes: list[tuple[int, int]] = []

        def obj(num: int, body: bytes) -> None:
            offsets[num] = len(out)
            out.extend(f"{num} 0 obj\n".encode() + body + b"\nendobj\n")

        obj(1, f"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [{fields}] /SigFlags 3 >> >>".encode())
        obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        annots = " ".join(f"{o} 0 R" for o in sig_objs)
        obj(3, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> /Annots [{annots}] >>".encode())
        obj(4, b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        obj(5, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        for i, field_obj in enumerate(sig_objs):
            val = field_obj + 1
            obj(field_obj, f"<< /Type /Annot /Subtype /Widget /FT /Sig /T (Signature{i + 1}) /Rect [0 0 0 0] /F 132 /P 3 0 R /V {val} 0 R >>".encode())
            a, b, c = ranges[i] if ranges else (0, 0, 0)
            head = b"<< /Type /Sig /Filter /Adobe.PPKLite /SubFilter /adbe.pkcs7.detached /ByteRange "
            offsets[val] = len(out)
            out.extend(f"{val} 0 obj\n".encode() + head)
            out.extend(f"[0 {a:010d} {b:010d} {c:010d}]".encode() + b" /Contents <")
            start = len(out) - 1
            out.extend(b"0" * 64 + b"> >>\nendobj\n")
            holes.append((start, len(out) - len(b" >>\nendobj\n")))
        xref = len(out)
        size = max(offsets) + 1
        out.extend(f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n")
        for num in range(1, size):
            out.extend(f"{offsets[num]:010d} 00000 n \n".encode() if num in offsets else b"0000000000 65535 f \n")
        out.extend(f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        return bytes(out), holes

    first, holes = build(None)
    ranges = [(s, e, len(first) - e) for s, e in holes]
    final, _ = build(ranges)
    return final


# ---------------------------------------------------------------------------------------------
# Simulated adapters
# ---------------------------------------------------------------------------------------------
_RM = r"RM\s?([\d,]+(?:\.\d{2})?)"


def _money(text: str, pattern: str) -> Decimal | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return Decimal(m.group(1).replace(",", "")) if m else None


def _find_date(text: str) -> dt.date | None:
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})", text)
    if m and m.group(2).lower() in MONTHS:
        return dt.date(int(m.group(3)), MONTHS.index(m.group(2).lower()) + 1, int(m.group(1)))
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


class HeuristicClassifier:
    """Keyword and regex extraction for the sample contracts. Replace with Ollama in production."""

    def classify(self, text: str) -> Classification:
        low = text.lower()
        date = _find_date(text)
        item: str | None = None
        fin: dict[str, Any] = {}
        kind: str | None = None
        if "share transfer" in low:
            item, kind = "ITEM_32_SHARES", "SHARE_TRANSFER"
            fin = {"consideration_or_loan_rm": _money(text, r"consideration[^\n]*?" + _RM),
                   "net_tangible_assets_rm": _money(text, r"net tangible assets[^\n]*?" + _RM)}
        elif "shareholders agreement" in low or "intercompany" in low:
            item = "ITEM_4_GENERAL"
            kind = "SHAREHOLDER" if "shareholders" in low else "INTERCOMPANY"
        elif "service agreement" in low:
            item, kind = "ITEM_22_AGREEMENT", "SERVICE"
            fin = {"consideration_or_loan_rm": _money(text, r"contract sum\s+" + _RM),
                   "fixed_tenure": "fixed term" in low}
        elif "unsecured loan" in low:
            item, kind = "ITEM_22_AGREEMENT", "LOAN"
            fin = {"consideration_or_loan_rm": _money(text, r"principal(?:\s+sum)?\s+" + _RM),
                   "fixed_tenure": "fixed term" in low}
        elif "tenancy" in low or "lease" in low:
            item = "ITEM_49_LEASE"
            months = re.search(r"(\d+)\s*months", low)
            fin = {"monthly_rental_rm": _money(text, r"rent\s+of\s+" + _RM) or _money(text, r"rent\s+" + _RM),
                   "lease_duration_months": int(months.group(1)) if months else None}
        elif "facility" in low or "loan" in low:
            item = "ITEM_22_SECURITY"
            fin = {"consideration_or_loan_rm": _money(text, r"principal(?:\s+sum)?\s+" + _RM),
                   "security_kind": "SECURED_LOAN" if "secured" in low else "OTHER"}
        elif "employment" in low:
            item = "ITEM_4_EMPLOYMENT"
            fin = {"monthly_salary_rm": _money(text, r"(?:salary|wage)s?\s+of\s+" + _RM)}
        elif "transfer" in low:
            item = "ITEM_32A_MOT"
            fin = {"consideration_or_loan_rm": _money(text, r"consideration[^\n]*?" + _RM)}
        owner = re.search(r"contract owner:\s*([^\n.]+)", text, re.IGNORECASE)
        kind = kind or {"ITEM_49_LEASE": "TENANCY", "ITEM_22_SECURITY": "SECURITY", "ITEM_4_EMPLOYMENT": "EMPLOYMENT",
                        "ITEM_32A_MOT": "SHARE_TRANSFER"}.get(item or "")
        if item is None or date is None:
            raise GateFailure("CLASSIFIER_SCHEMA", "Demo classifier could not recognise this document")
        module = {"ITEM_49_LEASE": "SEWA_PAJAKAN", "ITEM_22_SECURITY": "SEKURITI",
                  "ITEM_4_EMPLOYMENT": "PENYETEMAN_AM", "ITEM_32A_MOT": "PINDAH_MILIK_HARTANAH",
                  "ITEM_4_GENERAL": "PERJANJIAN_AM", "ITEM_22_AGREEMENT": "PERJANJIAN_PINJAMAN",
                  "ITEM_32_SHARES": "PEMINDAHAN_SAHAM"}[item]
        return Classification.model_validate({
            "bnds_module": module, "first_schedule_item": item, "requires_jpph_valuation": False,
            "is_foreign_buyer_or_entity": False, "financials": {k: v for k, v in fin.items() if v is not None},
            "parties": [p.model_dump() for p in (NEW_CLIENT_PARTIES if "demo omega" in low else DEMO_PARTIES)],
            "execution_date": date,
            "document_kind": kind, "contract_owner": owner.group(1).strip() if owner else None,
            "confidence": 0.72 if "illegible" in low else 0.99,
        })


class LyingClassifier:
    """Wraps another classifier and corrupts one figure, to show the grounding gate catching it."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def classify(self, text: str) -> Classification:
        c: Classification = self.inner.classify(text)
        fin = c.financials.model_copy(update={"monthly_rental_rm": Decimal("900.00")})
        return c.model_copy(update={"financials": fin})


def _ceil(x: Fraction) -> int:
    return math.ceil(x)


def portal_total(module: str, f: dict[str, str]) -> Decimal:
    """The simulated portal's own calculation, written independently of the engine."""
    dup = int(f.get("duplicate_copies", "0"))
    if module == "PENYETEMAN_AM":
        base = 0 if Decimal(f["monthly_wage"]) <= 3000 else 10
        return Decimal(base + (0 if base == 0 else dup * 10))
    if module == "SEWA_PAJAKAN":
        months = int(f["lease_months"])
        rate = 1 if months <= 12 else 3 if months <= 36 else 5 if months <= 60 else 7
        return Decimal(max(_ceil(Fraction(Decimal(f["monthly_rent"])) * 12 / 250) * rate, 10) + dup * 10)
    if module == "PERJANJIAN_AM":
        return Decimal(10 + dup * 10)
    if module == "PERJANJIAN_PINJAMAN":
        rate = 5 if f.get("fixed_tenure") == "Y" else 10
        return Decimal(max(_ceil(Fraction(Decimal(f["principal_or_consideration"])) / 1000) * rate, 10) + dup * 10)
    if module == "PEMINDAHAN_SAHAM":
        value = max(Decimal(f["principal_or_consideration"]), Decimal(f["net_tangible_assets"]))
        return Decimal(max(_ceil(Fraction(value) / 1000) * 3, 10) + dup * 10)
    if module == "SEKURITI":
        return Decimal(max(_ceil(Fraction(Decimal(f["principal_or_consideration"])) / 1000) * 5, 10) + dup * 10)
    value = max(Decimal(f["principal_or_consideration"]), Decimal(f.get("market_value", "0")))
    if f.get("is_foreign") == "Y":
        return Decimal(max(_ceil(Fraction(value) / 100) * (8 if f.get("is_residential") == "Y" else 4), 10) + dup * 10)
    total = 0
    bands: tuple[tuple[int, int | None, int], ...] = (
        (0, 100_000, 1), (100_000, 500_000, 2), (500_000, 1_000_000, 3), (1_000_000, None, 4))
    for lo, hi, rate in bands:
        if value > lo:
            top = value if hi is None else Decimal(min(value, Decimal(hi)))
            total += _ceil(Fraction(top - lo) / 100) * rate
    return Decimal(max(total, 10) + dup * 10)


class SimulatedPortal:
    """Stand-in for MyTax. Computes its own totals, issues bills, and releases the certificate only
    for a bill that the simulated payment provider reports as paid."""

    def __init__(self, skew: Decimal = Decimal("0")) -> None:
        self.skew = skew
        self.submits = 0
        self._drafts: dict[str, tuple[str, Decimal]] = {}
        self._bill_of: dict[str, str] = {}
        self.bills: dict[str, Decimal] = {}
        self.stamped: set[str] = set()
        self.payments: SimulatedPayments | None = None
        self.hide_stamp = False
        self.qr_host = "verify.demo.invalid"
        self.qr_mode = "ok"  # ok | no-qr | foreign-host | invalid | hidden

    async def draft(self, module: str, fields: dict[str, str], parties: list[dict[str, str]]) -> DraftResult:
        total = (portal_total(module, fields) + self.skew).quantize(Decimal("0.01"))
        draft_id = f"DEMO-{len(self._drafts) + 1:04d}"
        self._drafts[draft_id] = (module, total)
        return DraftResult(draft_id, total, {})

    async def submit(self, draft_id: str) -> SubmitResult:
        self.submits += 1
        bill = f"DEMO-BILL-{self.submits:04d}"
        self.bills[bill] = self._drafts[draft_id][1]
        self._bill_of[draft_id] = bill
        return SubmitResult(bill, f"DEMO-VA-{self.submits:04d}")

    async def download_sijil(self, draft_id: str) -> bytes:
        bill = self._bill_of.get(draft_id)
        if self.payments is not None and not (bill and (self.bills[bill] == 0 or self.payments.is_paid(bill))):
            raise GateFailure("SIJIL_NOT_AVAILABLE", "The certificate is issued only after payment", "HALT")
        if bill:
            self.stamped.add(bill)
        host = "evil.example" if self.qr_mode == "foreign-host" else self.qr_host
        url = None if self.qr_mode == "no-qr" or not bill else f"https://{host}/certificate/{bill}"
        return make_certificate(f"SIMULATED SIJIL SETEM (demo)\nDraft {draft_id}\nNot a real certificate.", url)

    async def verify_qr(self, url: str) -> QrVerification:
        if self.qr_mode == "hidden":
            return QrVerification(None, detail="page not readable")
        bill = urlsplit(url).path.rsplit("/", 1)[-1]
        if self.qr_mode == "invalid" or bill not in self.stamped:
            return QrVerification(False, detail="not valid")
        return QrVerification(True, f"CERT-{bill}", self.bills[bill])

    async def bill_status(self, bill_no: str) -> PortalBill:
        return PortalBill(None if self.hide_stamp else bill_no in self.stamped)


class SimulatedPayments:
    """Stand-in for the payment provider. Honours idempotency keys and checks the amount against the
    bill, and can fail the two ways real payments fail: before money moves and after."""

    def __init__(self, portal: SimulatedPortal) -> None:
        self.portal = portal
        portal.payments = self
        self.paid: dict[str, tuple[str, Decimal]] = {}
        self._keys: dict[str, PaymentResult] = {}
        self.fail_next: str | None = None  # "reject" | "timeout-before" | "lost-reply"
        self.calls = 0

    def is_paid(self, bill_no: str) -> bool:
        return bill_no in self.paid

    async def pay(
        self, *, idempotency_key: str, bill_no: str, virtual_account: str | None, amount_rm: Decimal
    ) -> PaymentResult:
        self.calls += 1
        mode, self.fail_next = self.fail_next, None
        if idempotency_key in self._keys:  # replay: same answer, never a second payment
            return self._keys[idempotency_key]
        if self.portal.bills.get(bill_no) != amount_rm:
            raise GateFailure("PAYMENT_REJECTED", "Bill or amount does not match the portal", "HALT")
        if mode == "reject":
            raise GateFailure("PAYMENT_REJECTED", "Insufficient funds (simulated)", "HALT")
        if mode == "timeout-before":
            raise GateFailure("PAYMENT_UNCERTAIN", "Timed out before the provider processed it", "HALT")
        result = PaymentResult(f"PAY-{len(self.paid) + 1:05d}", amount_rm, "PAID")
        self.paid[bill_no] = (result.reference, amount_rm)
        self._keys[idempotency_key] = result
        if mode == "lost-reply":
            raise GateFailure("PAYMENT_UNCERTAIN", "Reply lost after the provider processed it", "HALT")
        return result

    async def status(self, bill_no: str) -> BillStatus:
        if bill_no in self.paid:
            ref, amount = self.paid[bill_no]
            return BillStatus("PAID", amount, ref)
        return BillStatus("UNPAID")


class SimulatedSigner:
    """Stand-in for the e-signature gateway. Call ``sign(envelope_id)`` to play the signers."""

    def __init__(self) -> None:
        self._docs: dict[str, tuple[bytes, int]] = {}
        self.state: dict[str, str] = {}

    async def request(self, *, pdf: bytes, signers: list[Signer], reference: str) -> str:
        env = f"ENV-{len(self._docs) + 1:06d}"
        self._docs[env] = (pdf, len(signers))
        self.state[env] = "PENDING"
        return env

    def sign(self, envelope_id: str) -> None:
        self.state[envelope_id] = "COMPLETED"

    def decline(self, envelope_id: str) -> None:
        self.state[envelope_id] = "DECLINED"

    async def status(self, envelope_id: str) -> SigningStatus:
        pdf, count = self._docs[envelope_id]
        state = self.state[envelope_id]
        if state != "COMPLETED":
            return SigningStatus(state, None, 0, count)  # type: ignore[arg-type]
        text = PdfTextExtractor().extract(pdf)
        return SigningStatus("COMPLETED", make_signed_pdf(text, count), count, count)


class LocalFolderS3:
    """Stands in for S3 by writing files. It records the requested lock mode but enforces nothing."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def put_object(self, **kw: Any) -> dict[str, Any]:
        path = self.root / kw["Key"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(kw["Body"])
        path.with_suffix(path.suffix + ".lock").write_text(str(kw.get("ObjectLockMode")))
        return {"VersionId": "demo"}

    def head_object(self, **kw: Any) -> dict[str, Any]:
        lock = (self.root / (kw["Key"] + ".lock")).read_text()
        return {"ObjectLockMode": lock}


class FormatOnlyVerifier:
    def verify(self, party: Party) -> bool:
        return True


# ---------------------------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------------------------
def build_demo(
    workdir: Path,
    skew: Decimal = Decimal("0"),
    classifier: Any = None,
    today: dt.date | None = None,
    engine_token: str | None = None,
    webhook_secret: str | None = None,
    with_payments: bool = False,
    **settings_kw: Any,
) -> tuple[Pipeline, Settings, SimulatedPortal, NullNotifier]:
    rules_path = Path(__file__).resolve().parents[2] / "rules" / "stamp_rules.json"
    rules, sha = load_rules(rules_path)
    live = {**rules.phase_start, **{k: dt.date(2026, 1, 1) for k in ("ITEM_4_GENERAL", "ITEM_22_AGREEMENT", "ITEM_32_SHARES")}}
    rules = rules.model_copy(update={"signoff": Signoff(by="DEMO ONLY", role="demo", date=dt.date.today()),
                                     "phase_start": live})  # demo only: the new items are NOT live in the real rules file
    base: dict[str, Any] = dict(
        mode="live", engine_token=engine_token or secrets.token_hex(32),
        webhook_secret=webhook_secret or secrets.token_hex(32), rules_path=str(rules_path),
        staging_dir=str(workdir / "staging"), ollama_url="", ollama_model="", qdrant_url=None,
        s3_bucket="demo", s3_kms_key_id=None, database_url=None, telegram_bot_token=None, telegram_chat_id=None,
        tin_verify_url=None, tin_verify_token=None, rpa_storage_state="", rpa_selectors_path="",
        maker_checker_threshold_rm=Decimal("10000"), phase2_enabled=False, max_body_bytes=28 * 1024 * 1024,
        qr_verify_hosts="verify.demo.invalid",
    )
    base.update(settings_kw)
    settings = Settings(**base)
    portal, notifier = SimulatedPortal(skew), NullNotifier()
    payments = SimulatedPayments(portal) if with_payments else None
    pipeline = Pipeline(
        clients=ClientDirectory(workdir / "clients.sqlite3"),
        settings=settings, rules=rules, rules_sha256=sha, extractor=PdfTextExtractor(),
        classifier=classifier or HeuristicClassifier(), verifier=FormatOnlyVerifier(), rpa=portal,
        vault=Vault(LocalFolderS3(workdir / "demo-vault-NOT-WORM"), "demo", None, workdir / "sijil-cache"),
        audit=MemoryAuditStore(), store=StagingStore(workdir / "staging"), notifier=notifier,
        today=(lambda: today) if today else None,
        payments=payments, ledger=Ledger(workdir / "ledger" / "ledger.jsonl") if with_payments else None,
    )
    return pipeline, settings, portal, notifier


class Kit:
    """Everything needed to run batches against the simulated world."""

    def __init__(self, workdir: Path, **kw: Any) -> None:
        self.pipeline, self.settings, self.portal, self.notifier = build_demo(workdir, with_payments=True, **kw)
        assert self.portal.payments is not None
        self.payments: SimulatedPayments = self.portal.payments
        self.orchestrator = Orchestrator(self.pipeline)
        self.batches = BatchService(self.orchestrator)


# ---------------------------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------------------------
def _b64(kind: str, signed_on: dt.date) -> str:
    return base64.b64encode(make_pdf(sample_text(kind, signed_on))).decode()


async def run_scenarios() -> list[tuple[str, str, str]]:
    signed = dt.date.today() - dt.timedelta(days=2)
    rows: list[tuple[str, str, str]] = []

    async def full(label: str, kind: str, approve: bool = False, **kw: Any) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pipe, _s, _p, _n = build_demo(Path(tmp), **kw)
            res = await pipe.run_to_parity(_b64(kind, signed), "demo")
            if not res.ok or res.halt:
                halt = res.halt or {}
                rows.append((label, "HALTED", f"{halt.get('code')}: {halt.get('message')}"))
                return
            detail = f"duty RM{res.data['engine_total']} = LHDN RM{res.data['lhdn_total']}"
            sub = await pipe.submit(res.txn_id)
            if sub.state == "AWAITING_APPROVAL" and approve:
                detail += "; submit blocked until a checker approved"
                sub = await pipe.submit(res.txn_id, approved_by="Demo Checker")
            if not sub.ok:
                rows.append((label, "BLOCKED", f"{detail}; {(sub.halt or {}).get('code')}"))
                return
            done = await pipe.complete(res.txn_id)
            chain = verify_chain(pipe.audit.records(res.txn_id))
            rows.append((label, "VAULTED" if done.ok else "FAILED", f"{detail}; audit chain valid: {chain}"))

    await full("1 Lease RM1,500 x 12 months", "lease")
    await full("2 Loan RM5,000,000 (maker-checker)", "loan", approve=True)
    await full("3 Employment RM2,800 (exempt)", "employment")
    await full("4 Portal differs by RM0.01", "lease", skew=Decimal("0.01"))
    await full("5 Model returns wrong rent", "lease", classifier=LyingClassifier(HeuristicClassifier()))
    await full("6 Property transfer before 1 Jan 2027", "transfer")
    return rows


async def run_batch_demo(resolve: bool = True) -> dict[str, Any]:
    """Six documents in one batch: clean ones run alone, unsure/high-value ones wait for people."""
    signed = dt.date.today() - dt.timedelta(days=2)
    lease = make_pdf(sample_text("lease", signed))
    docs: list[tuple[str, bytes | None]] = [
        ("lease-1500-x12.pdf", lease),
        ("employment-2800.pdf", make_pdf(sample_text("employment", signed))),
        ("loan-5m.pdf", make_pdf(sample_text("loan", signed))),
        ("lease-poor-scan.pdf", make_pdf(sample_text("blurry", signed))),
        ("lease-1500-x12-again.pdf", lease),
        ("corrupt.pdf", b"this is not a pdf"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        kit = Kit(Path(tmp))
        batch = await kit.batches.create("demo-operator", "demo", docs)
        first = await kit.batches.run(batch.batch_id)
        out: dict[str, Any] = {"first_pass": first}
        if resolve:
            for item in kit.pipeline.work_items():
                if item["state"] == "REVIEW" and item["resolvable"]:
                    kit.pipeline.review_resolve(item["txn_id"], "Aisha (reviewer)", "approve", attest=True,
                                                note="checked against the original scan")
                    await kit.orchestrator.advance(item["txn_id"])
            for item in kit.pipeline.work_items():
                if item["state"] == "AWAITING_APPROVAL":
                    await kit.orchestrator.advance(item["txn_id"], approved_by="Daniel (checker)")
            out["final"] = kit.batches.summary(batch.batch_id)
        out["ledger_rows"] = len(kit.pipeline.ledger.rows()) if kit.pipeline.ledger else 0
        out["ledger_valid"] = bool(kit.pipeline.ledger and kit.pipeline.ledger.verify())
        out["paid"] = {b: str(a) for b, (_r, a) in kit.payments.paid.items()}
        return out


def print_batch(summary: dict[str, Any], title: str) -> None:
    print(f"\n{title}")
    t = summary["totals"]
    print(f"  complete {t['complete']} · waiting for people {t['waiting_for_people']} · failed {t['failed']} "
          f"· duty completed RM{t['duty_completed_rm']}")
    for d in summary["documents"]:
        score = f"{d['score']:.2f}" if d.get("score") is not None else " -- "
        note = f"{d.get('code')}: {d.get('message')}" if d.get("code") else ""
        dup = " (duplicate)" if d.get("duplicate_in_batch") else ""
        print(f"  {d['name']:<28} {d['state']:<18} score {score}  duty {d.get('duty_rm') or '--':>10}  {note}{dup}")


def print_rows(rows: list[tuple[str, str, str]]) -> None:
    print(BANNER, "\n")
    for label, status, detail in rows:
        print(f"{label:<42} {status:<14} {detail}")


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------
CREDS = Path(".stsds-demo/creds.json")


def serve(port: int) -> None:
    import shutil

    import uvicorn

    from .api import create_app
    from .users import UserDirectory, hash_token

    work = Path(".stsds-demo")
    work.mkdir(exist_ok=True)
    for stale in ("staging", "ledger", "sijil-cache", "demo-vault-NOT-WORM"):  # every run starts clean
        shutil.rmtree(work / stale, ignore_errors=True)
    token, secret = secrets.token_hex(20), secrets.token_hex(32)
    reviewer, checker = secrets.token_hex(20), secrets.token_hex(20)
    kit = Kit(work, engine_token=token, webhook_secret=secret)
    users = UserDirectory([
        {"name": "Demo Reviewer", "roles": ["reviewer"], "token_sha256": hash_token(reviewer)},
        {"name": "Demo Checker", "roles": ["checker"], "token_sha256": hash_token(checker)},
    ])
    CREDS.write_text(json.dumps({"token": token, "secret": secret, "port": port,
                                 "reviewer_token": reviewer, "checker_token": checker}))
    CREDS.chmod(0o600)
    print(BANNER)
    print(f"Serving on http://127.0.0.1:{port}")
    print(f"  Reviewer console : http://127.0.0.1:{port}/console")
    print(f"  Reviewer token   : {reviewer}   (role: reviewer)")
    print(f"  Checker token    : {checker}   (role: checker)")
    print("In another terminal:  python -m stsds.demo send demo-samples/*.pdf --approve")
    uvicorn.run(create_app(kit.pipeline, kit.settings, users), host="127.0.0.1", port=port, log_level="warning")


def send(paths: list[str], approve: bool = False) -> None:
    import time

    import httpx

    creds = json.loads(CREDS.read_text())
    base = f"http://127.0.0.1:{creds['port']}"
    ops = {"Authorization": f"Bearer {creds['token']}"}
    docs = [{"name": Path(p).name, "pdf_base64": base64.b64encode(Path(p).read_bytes()).decode()} for p in paths]
    made = httpx.post(f"{base}/v1/batches", headers=ops, json={"source": "demo-cli", "documents": docs}, timeout=60)
    made.raise_for_status()
    batch = made.json()
    print(f"batch {batch['batch_id']}: {batch['accepted']} accepted, {batch['rejected_at_intake']} rejected at intake")

    def fetch() -> dict[str, Any]:
        for _ in range(60):
            summary: dict[str, Any] = httpx.get(f"{base}/v1/batches/{batch['batch_id']}", headers=ops, timeout=30).json()
            if summary["totals"]["in_progress"] == 0:
                return summary
            time.sleep(0.5)
        return summary

    summary = fetch()
    print_batch(summary, "After the automatic pass")
    if approve:
        checker = {"Authorization": f"Bearer {creds['checker_token']}"}
        for d in summary["documents"]:
            if d["state"] == "AWAITING_APPROVAL":
                httpx.post(f"{base}/v1/approvals/{d['txn_id']}", headers=checker, timeout=60)
        summary = fetch()
        print_batch(summary, "After the checker approved the high-value filings")
    waiting = [d for d in summary["documents"] if d["state"] == "REVIEW"]
    if waiting:
        print(f"\n{len(waiting)} document(s) are waiting for a reviewer: open http://127.0.0.1:{creds['port']}/console")
        print(f"and sign in with the reviewer token ({CREDS}).")
    ledger = httpx.get(f"{base}/v1/ledger?batch_id={batch['batch_id']}", headers=ops, timeout=30).json()
    print(f"\nledger: {len(ledger['rows'])} rows for this batch, chain valid: {ledger['chain_valid']}")


def write_samples() -> None:
    out = Path("demo-samples")
    out.mkdir(exist_ok=True)
    signed = dt.date.today() - dt.timedelta(days=2)
    for kind in ("lease", "loan", "employment", "transfer", "blurry"):
        (out / f"{kind}.pdf").write_bytes(make_pdf(sample_text(kind, signed)))
    print(f"Wrote 5 sample contracts to {out}/ (dated {signed.isoformat()}, so the 30-day clock is live)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stsds.demo", description=BANNER)
    sub = ap.add_subparsers(dest="cmd")
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=8080)
    sd = sub.add_parser("send", help="submit one or more PDFs as a batch to a running 'serve'")
    sd.add_argument("files", nargs="+")
    sd.add_argument("--approve", action="store_true", help="let the demo checker approve high-value filings")
    sub.add_parser("sample")
    bt = sub.add_parser("batch", help="run a mixed six-document batch through the whole loop")
    bt.add_argument("--no-resolve", action="store_true", help="stop after the first pass (leave people's work pending)")
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        write_samples()
        serve(args.port)
    elif args.cmd == "send":
        send(args.files, args.approve)
    elif args.cmd == "sample":
        write_samples()
    elif args.cmd == "batch":
        print(BANNER)
        result = asyncio.run(run_batch_demo(resolve=not args.no_resolve))
        print_batch(result["first_pass"], "After the automatic pass")
        if "final" in result:
            print_batch(result["final"], "After the reviewer and checker acted")
        print(f"\nLedger: {result['ledger_rows']} rows, chain valid: {result['ledger_valid']}; "
              f"{len(result['paid'])} bills paid")
    else:
        print_rows(asyncio.run(run_scenarios()))
    return 0


if __name__ == "__main__":
    sys.exit(main())

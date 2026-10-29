"""Hard gates. Every gate fails closed and returns a routing decision, never a silent pass.

  * check_pdf          FR-01  size <= 20MB, real PDF, readable, not encrypted
  * sec47_clock        FR-01  30-day statutory timer from execution
  * confidence_gate    FR-02  classifier confidence floor
  * grounding_gate     FR-02  every extracted figure must appear verbatim in the OCR text
  * jpph_gate / phase_gate / preflight_parties   FR-03
  * parity_gate        FR-04  engine total must equal LHDN's Rumusan Pengiraan to the sen
"""

from __future__ import annotations

import datetime as dt
import io
import re
from decimal import Decimal, InvalidOperation
from typing import Protocol

import httpx
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .rules import Rules
from .schemas import Classification, ClockStatus, IdType, Item, Party

MAX_PDF_BYTES = 20 * 1024 * 1024  # LHDN ceiling
CONFIDENCE_FLOOR = 0.95
URGENT_DAYS = 7


class GateFailure(Exception):
    """A gate refused to proceed. ``route`` is REVIEW (human) or HALT (stop, alert)."""

    def __init__(self, code: str, message: str, route: str = "REVIEW") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.route = route


# --- FR-01 -------------------------------------------------------------------------------------
def check_pdf(data: bytes, max_bytes: int = MAX_PDF_BYTES) -> int:
    """Validate the document and return its page count."""
    if len(data) == 0:
        raise GateFailure("PDF_EMPTY", "Document is empty", "HALT")
    if len(data) > max_bytes:
        raise GateFailure("PDF_TOO_LARGE", "Document exceeds the 20MB LHDN ceiling", "HALT")
    if not data.startswith(b"%PDF-"):
        raise GateFailure("PDF_MAGIC", "File is not a PDF", "HALT")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise GateFailure("PDF_ENCRYPTED", "Encrypted PDFs are not accepted", "REVIEW")
        pages = len(reader.pages)
    except PyPdfError as exc:
        raise GateFailure("PDF_UNREADABLE", f"PDF could not be parsed: {exc}", "HALT") from exc
    if pages == 0:
        raise GateFailure("PDF_NO_PAGES", "PDF has no pages", "HALT")
    return pages


def sec47_clock(
    execution_date: dt.date,
    today: dt.date,
    received_in_malaysia: dt.date | None = None,
) -> ClockStatus:
    """30 days from execution (or from receipt in Malaysia for instruments signed abroad)."""
    start = received_in_malaysia or execution_date
    deadline = start + dt.timedelta(days=30)
    remaining = (deadline - today).days
    state = "OVERDUE" if remaining < 0 else ("URGENT" if remaining <= URGENT_DAYS else "OK")
    return ClockStatus(
        execution_date=execution_date, deadline=deadline, days_remaining=remaining, state=state
    )


def clock_gate(clock: ClockStatus) -> None:
    if clock.state == "OVERDUE":
        raise GateFailure(
            "SEC47_OVERDUE",
            "30-day window has passed: late-stamping penalty regime applies. Manual review required.",
            "REVIEW",
        )


# --- FR-02 -------------------------------------------------------------------------------------
def confidence_gate(c: Classification, floor: float = CONFIDENCE_FLOOR) -> None:
    if c.confidence < floor:
        raise GateFailure(
            "LOW_CONFIDENCE", f"Classifier confidence {c.confidence:.2f} is below {floor:.2f}"
        )


def _variants(value: Decimal) -> set[str]:
    q = value.quantize(Decimal("0.01"))
    plain = f"{q:.2f}"
    whole = f"{int(q)}" if q == q.to_integral_value() else plain
    grouped = f"{q:,.2f}"
    grouped_whole = f"{int(q):,}" if q == q.to_integral_value() else grouped
    return {plain, whole, grouped, grouped_whole}


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text)


_MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
_BM_MONTHS = [
    "januari", "februari", "mac", "april", "mei", "jun",
    "julai", "ogos", "september", "oktober", "november", "disember",
]


def _date_in_text(d: dt.date, text: str) -> bool:
    """The execution date drives the 30-day clock and the rule window, so it must be in the text."""
    y, m, day = d.year, d.month, d.day
    numeric = [
        rf"(?<!\d){y}-{m:02d}-{day:02d}(?!\d)",
        rf"(?<!\d)0?{day}[/.-]0?{m}[/.-]{y}(?!\d)",
    ]
    names = f"(?:{_MONTHS[m - 1]}|{_MONTHS[m - 1][:3]}\\.?|{_BM_MONTHS[m - 1]})"
    worded = rf"(?<!\d)0?{day}(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?{names},?\s+{y}(?!\d)"
    american = rf"{names}\s+0?{day}(?:st|nd|rd|th)?,?\s+{y}(?!\d)"
    return any(re.search(p, text, re.IGNORECASE) for p in [*numeric, worded, american])


def grounding_report(c: Classification, ocr_text: str) -> tuple[list[str], int]:
    """Return (fields not found verbatim in the text, number of fields that were checked)."""
    text = _normalise(ocr_text)
    f = c.financials
    figures = {
        "monthly_salary_rm": f.monthly_salary_rm,
        "monthly_rental_rm": f.monthly_rental_rm,
        "consideration_or_loan_rm": f.consideration_or_loan_rm,
        "market_value_rm": f.market_value_rm,
        "net_tangible_assets_rm": f.net_tangible_assets_rm,
    }
    missing: list[str] = []
    checked = 1  # the execution date is always checked
    for name, value in figures.items():
        if value is None:
            continue
        checked += 1
        found = any(
            re.search(rf"(?<![\d,.]){re.escape(v)}(?![\d]|,\d|\.\d)", text) for v in _variants(value)
        )
        if not found:
            missing.append(name)
    if f.lease_duration_months is not None and not f.lease_indefinite:
        checked += 1
        months = f.lease_duration_months
        patterns = [rf"\b{months}\s*(?:months?|bulan)\b"]
        if months % 12 == 0:
            years = months // 12
            patterns.append(rf"\b{years}\s*(?:years?|tahun)\b")
            words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}
            if years in words:
                patterns.append(rf"\b{words[years]}\s*(?:\(\s*{years}\s*\)\s*)?(?:years?|tahun)\b")
        if not any(re.search(p, text, re.IGNORECASE) for p in patterns):
            missing.append("lease_duration_months")
    if not _date_in_text(c.execution_date, text):
        missing.append("execution_date")
    return missing, checked


def grounding_gate(c: Classification, ocr_text: str) -> None:
    """Every monetary figure the model returned must appear verbatim in the source text.

    This is the control that prevents a hallucinated number from being taxed. The check is
    on digit strings with token boundaries, so '1,500' does not match inside '11,500'.
    """
    missing, _checked = grounding_report(c, ocr_text)
    if missing:
        raise GateFailure(
            "UNGROUNDED_FIGURE",
            "Figures not found verbatim in the document: " + ", ".join(sorted(missing)),
        )


# --- FR-03 -------------------------------------------------------------------------------------
_TIN_RE = re.compile(r"^[A-Z]{1,3}\d{6,12}$")
_BRN_RE = re.compile(r"^[A-Z0-9-]{6,20}$")
_PASSPORT_RE = re.compile(r"^[A-Z0-9]{6,15}$")


def _valid_nric(value: str) -> bool:
    digits = value.replace("-", "")
    if not re.fullmatch(r"\d{12}", digits):
        return False
    yy, mm, dd = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
    for century in (1900, 2000):
        try:
            dt.date(century + yy, mm, dd)
            return True
        except ValueError:
            continue
    return False


class TinVerifier(Protocol):
    def verify(self, party: Party) -> bool: ...


class HttpTinVerifier:
    """Calls an LHDN-authorised TIN verification endpoint. Unconfigured means fail closed."""

    def __init__(self, url: str | None, token: str | None, timeout: float = 10.0) -> None:
        self._url, self._token, self._timeout = url, token, timeout

    def verify(self, party: Party) -> bool:
        if not self._url or not self._token:
            raise GateFailure(
                "TIN_VERIFIER_UNCONFIGURED", "No TIN verification endpoint is configured", "HALT"
            )
        try:
            resp = httpx.post(
                self._url,
                json={"tin": party.tin, "idType": party.id_type.value, "idValue": party.id_number},
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return bool(resp.json().get("valid") is True)
        except (httpx.HTTPError, ValueError) as exc:
            raise GateFailure("TIN_VERIFIER_ERROR", f"TIN verification failed: {exc}", "HALT") from exc


def preflight_parties(parties: list[Party], verifier: TinVerifier) -> None:
    """Every transacting party needs a valid TIN and a valid NRIC/BRN/passport."""
    for p in parties:
        if not p.tin or not _TIN_RE.match(p.tin.upper()):
            raise GateFailure("TIN_FORMAT", f"Missing or malformed TIN for {p.role}")
        if p.id_type is IdType.NRIC and not _valid_nric(p.id_number):
            raise GateFailure("NRIC_INVALID", f"Invalid NRIC for {p.role}")
        if p.id_type is IdType.BRN and not _BRN_RE.match(p.id_number.upper()):
            raise GateFailure("BRN_INVALID", f"Invalid BRN for {p.role}")
        if p.id_type is IdType.PASSPORT and not _PASSPORT_RE.match(p.id_number.upper()):
            raise GateFailure("PASSPORT_INVALID", f"Invalid passport number for {p.role}")
        if not verifier.verify(p):
            raise GateFailure("TIN_REJECTED", f"LHDN did not validate the TIN for {p.role}")


def jpph_gate(c: Classification) -> None:
    if c.requires_jpph_valuation:
        raise GateFailure(
            "JPPH_VALUATION", "JPPH valuation required: formal route, not self-assessment"
        )


def phase_gate(
    item: Item,
    execution_date: dt.date,
    today: dt.date,
    rules: Rules,
    phase2_enabled: bool = False,
) -> None:
    start = rules.phase_start.get(item.value)
    if start is None:
        raise GateFailure("PHASE_UNKNOWN", f"No phase date configured for {item.value}", "HALT")
    if today < start and not (item is Item.ITEM_32A_MOT and phase2_enabled):
        raise GateFailure("PHASE_NOT_LIVE", f"{item.value} is not live until {start.isoformat()}")


# --- FR-04 -------------------------------------------------------------------------------------
_RM_RE = re.compile(r"^(?:RM|MYR)?\s*(-?\d{1,3}(?:,\d{3})*|-?\d+)(?:\.(\d{1,2}))?$", re.IGNORECASE)


def parse_rm(text: str) -> Decimal:
    """Parse a scraped RM figure exactly. Anything unusual raises, so parity cannot pass on junk."""
    cleaned = text.strip().replace(" ", " ")
    m = _RM_RE.match(cleaned)
    if not m:
        raise GateFailure("PARITY_UNPARSEABLE", f"Could not read LHDN figure: {text!r}", "HALT")
    whole, frac = m.group(1).replace(",", ""), (m.group(2) or "0").ljust(2, "0")
    try:
        return Decimal(f"{whole}.{frac}")
    except InvalidOperation as exc:  # pragma: no cover - regex already constrains the format
        raise GateFailure("PARITY_UNPARSEABLE", f"Could not read LHDN figure: {text!r}", "HALT") from exc


def parity_gate(engine_total: Decimal, lhdn_total: Decimal | None) -> None:
    """Halt on any difference of RM0.01 or more. Submit is a binding Deemed Assessment (Sec. 36)."""
    if lhdn_total is None:
        raise GateFailure("PARITY_MISSING", "LHDN Rumusan Pengiraan figure was not captured", "HALT")
    if engine_total.quantize(Decimal("0.01")) != lhdn_total.quantize(Decimal("0.01")):
        raise GateFailure(
            "PARITY_MISMATCH",
            f"Engine RM{engine_total:.2f} differs from LHDN RM{lhdn_total:.2f}",
            "HALT",
        )

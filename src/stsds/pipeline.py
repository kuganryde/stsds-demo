"""Orchestration of blueprint nodes 1-10 with two hard safety gates.

Stages (each idempotent, each audited, each written to the transaction ledger):
  ingest -> classify (+score) -> [review] -> calculate -> preflight -> draft (parity gate)
         -> submit -> pay -> complete (Sijil + vault) -> verify
Nothing reaches the binding Submit unless: classification passed schema + confidence + grounding,
the document score cleared the auto threshold (or a named reviewer attested it), the Sec. 47 clock
is live, parties validated, no JPPH route, the phase is live, engine == LHDN to the sen, rules are
signed off, the worker is in live mode, and (above threshold or after a human edit) a different
person approved. Any failure halts the transaction, alerts an officer, and never clicks Submit.
Irreversible steps (submit, pay) write a marker BEFORE acting, so a crash leaves an explicit
"uncertain" state that is reconciled, never a silent retry.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import os
import re
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from . import engine
from .audit import AuditStore, verify_chain
from .classifier import Classifier
from .gates import (
    GateFailure,
    TinVerifier,
    check_pdf,
    clock_gate,
    confidence_gate,
    grounding_gate,
    grounding_report,
    jpph_gate,
    parity_gate,
    phase_gate,
    preflight_parties,
    sec47_clock,
)
from .ledger import Ledger
from .notify import Notifier
from .ocr import TextExtractor
from .payment import PaymentPort
from .qr import check_qr_url, parse_hosts, read_qr_urls
from .rpa import MyTaxPort
from .rules import Rules
from .schemas import CalcResult, Classification, ClockStatus
from .scoring import Assessment, assess
from .security import mask, valid_txn_id
from .settings import Settings
from .signing import signature_gate
from .vault import Vault

# Codes a human reviewer can resolve by checking the document and correcting the extraction.
# Everything else that routes to REVIEW (overdue, JPPH, phase not live, bad PDF) needs an officer's
# decision outside the extraction and can only be closed (rejected), never "approved through".
RESOLVABLE_CODES = frozenset({
    "LOW_CONFIDENCE", "LOW_SCORE", "UNGROUNDED_FIGURE", "CLASSIFIER_SCHEMA",
    "TIN_FORMAT", "NRIC_INVALID", "BRN_INVALID", "PASSPORT_INVALID", "TIN_REJECTED",
    "MONEY_PRECISION", "MISSING_WAGE", "MISSING_RENT", "MISSING_TERM", "MISSING_PRINCIPAL",
    "MISSING_VALUE", "RESIDENTIAL_FLAG", "SECURITY_KIND", "RATE_WINDOW", "RULE_WINDOW",
})
TERMINAL_STATES = frozenset({"COMPLETE", "REJECTED", "HALTED"})


class ReviewInfo(BaseModel):
    stage: str
    code: str
    message: str
    resolvable: bool


class TxnContext(BaseModel):
    txn_id: str
    doc_sha256: str
    source: str
    state: str = "INGESTED"
    batch_id: str | None = None
    name: str = ""
    received_in_malaysia: dt.date | None = None
    candidate: Classification | None = None
    assessment: Assessment | None = None
    review: ReviewInfo | None = None
    last_halt: dict[str, str] | None = None
    reviewed_by: str | None = None
    human_verified: bool = False
    needs_second_approval: bool = False
    payment: dict[str, str] | None = None
    payment_attempts: int = 0
    verification: dict[str, Any] | None = None
    classification: Classification | None = None
    clock: ClockStatus | None = None
    calc: CalcResult | None = None
    draft_id: str | None = None
    lhdn_total: Decimal | None = None
    bill_no: str | None = None
    virtual_account: str | None = None
    approved_by: str | None = None
    vault: dict[str, str] | None = None
    qr: dict[str, str] | None = None
    signatures: int | None = None
    stamped_on: dt.date | None = None


class StageResult(BaseModel):
    ok: bool
    txn_id: str
    state: str
    data: dict[str, Any] = {}
    halt: dict[str, str] | None = None


class StagingStore:
    """Atomic, 0600 JSON snapshots of transaction state plus the staged PDF and OCR text."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, txn_id: str, suffix: str) -> Path:
        if not valid_txn_id(txn_id):
            raise GateFailure("TXN_ID", "Invalid transaction id", "HALT")
        return self.root / f"{txn_id}{suffix}"

    def write(self, txn_id: str, suffix: str, data: bytes) -> None:
        path = self._path(txn_id, suffix)
        fd, tmp = tempfile.mkstemp(dir=self.root)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def read(self, txn_id: str, suffix: str) -> bytes | None:
        path = self._path(txn_id, suffix)
        return path.read_bytes() if path.exists() else None

    def save(self, ctx: TxnContext) -> None:
        self.write(ctx.txn_id, ".json", ctx.model_dump_json().encode())

    def load(self, txn_id: str) -> TxnContext | None:
        raw = self.read(txn_id, ".json")
        return TxnContext.model_validate_json(raw) if raw else None

    def delete_pdf(self, txn_id: str) -> None:
        self._path(txn_id, ".pdf").unlink(missing_ok=True)

    def claim_fingerprint(self, fingerprint: str, txn_id: str) -> str:
        """Atomically register ``fingerprint`` for ``txn_id``. Returns the owning transaction id.

        The first claimant wins (O_EXCL). A claim held by a REJECTED transaction may be taken over.
        """
        if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise GateFailure("FINGERPRINT", "Invalid instrument fingerprint", "HALT")
        folder = self.root / "fingerprints"
        folder.mkdir(exist_ok=True, mode=0o700)
        path = folder / fingerprint
        fd, tmp = tempfile.mkstemp(dir=folder)
        with os.fdopen(fd, "w") as fh:  # content is complete BEFORE the claim becomes visible
            fh.write(txn_id)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            for _ in range(2):
                try:
                    os.link(tmp, path)  # atomic create-if-absent
                    return txn_id
                except FileExistsError:
                    owner = path.read_text().strip()
                    existing = self.load(owner) if valid_txn_id(owner) else None
                    if owner == txn_id or (existing is not None and existing.state != "REJECTED"):
                        return owner
                    path.unlink(missing_ok=True)  # rejected owner: take over
            return path.read_text().strip()  # pragma: no cover - lost a takeover race; the winner owns it
        finally:
            Path(tmp).unlink(missing_ok=True)

    def list_ids(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json") if valid_txn_id(p.stem))


def bnds_fields(c: Classification) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Map the validated classification to portal field names (selector keys)."""
    f = c.financials
    fields = {"execution_date": c.execution_date.isoformat(), "duplicate_copies": str(f.duplicate_copies)}
    money = {
        "monthly_wage": f.monthly_salary_rm,
        "monthly_rent": f.monthly_rental_rm,
        "principal_or_consideration": f.consideration_or_loan_rm,
        "market_value": f.market_value_rm,
        "net_tangible_assets": f.net_tangible_assets_rm,
    }
    for key, value in money.items():
        if value is not None:
            fields[key] = f"{value:.2f}"
    if f.lease_duration_months is not None:
        fields["lease_months"] = str(f.lease_duration_months)
    if f.fixed_tenure is not None:
        fields["fixed_tenure"] = "Y" if f.fixed_tenure else "N"
    if c.first_schedule_item.value == "ITEM_32A_MOT":
        fields["is_foreign"] = "Y" if c.is_foreign_buyer_or_entity else "N"
        if c.is_residential_property is not None:
            fields["is_residential"] = "Y" if c.is_residential_property else "N"
    parties = [
        {"name": p.name, "tin": p.tin or "", "id_type": p.id_type.value, "id_number": p.id_number}
        for p in c.parties
    ]
    return fields, parties


def _flat(c: Classification | None) -> dict[str, str]:
    """Flatten a classification for diffing. Party details are reduced to a digest (no PII)."""
    if c is None:
        return {}
    data = json.loads(c.model_dump_json())
    out = {k: json.dumps(v, sort_keys=True) for k, v in data.items() if k not in {"financials", "parties", "evidence"}}
    out.update({f"financials.{k}": json.dumps(v) for k, v in data["financials"].items()})
    out["parties"] = hashlib.sha256(json.dumps(data["parties"], sort_keys=True).encode()).hexdigest()[:12]
    return out


def instrument_fingerprint(c: Classification) -> str:
    """Identity of the INSTRUMENT, independent of file bytes: a re-exported PDF of the same contract
    has a different SHA-256 but the same fingerprint, so it cannot be filed and paid twice."""
    data = json.loads(c.model_dump_json())
    ids = sorted(re.sub(r"[^A-Za-z0-9]", "", p["id_number"]).upper() for p in data["parties"])
    canonical = json.dumps(
        {"item": data["first_schedule_item"], "date": data["execution_date"], "fin": data["financials"],
         "ids": ids, "foreign": data["is_foreign_buyer_or_entity"], "res": data.get("is_residential_property")},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def classification_changes(before: Classification | None, after: Classification) -> dict[str, list[str]]:
    old, new = _flat(before), _flat(after)
    changed: dict[str, list[str]] = {}
    for key in sorted(set(old) | set(new)):
        if key in {"confidence", "evidence"}:
            continue
        if old.get(key) != new.get(key):
            hidden = key == "parties"  # never write party values to the audit trail
            changed[key] = ["(changed)", "(changed)"] if hidden else [old.get(key, "null"), new.get(key, "null")]
    return changed


class Pipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        rules: Rules,
        rules_sha256: str,
        extractor: TextExtractor,
        classifier: Classifier,
        verifier: TinVerifier,
        rpa: MyTaxPort,
        vault: Vault,
        audit: AuditStore,
        store: StagingStore,
        notifier: Notifier,
        today: Any = None,
        payments: PaymentPort | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        self.s, self.rules, self.rules_sha = settings, rules, rules_sha256
        self.extractor, self.classifier, self.verifier = extractor, classifier, verifier
        self.rpa, self.vault, self.audit, self.store, self.notifier = rpa, vault, audit, store, notifier
        self.payments, self.ledger = payments, ledger
        self._today = today or (lambda: dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date())

    # -- helpers -------------------------------------------------------------------------------
    def _log(
        self, ctx: TxnContext, stage: str, outcome: str, *, code: str = "", detail: str = "",
        actor: str = "system", strict: bool = False,
    ) -> None:
        """Write the ledger row for an outcome.

        ``strict`` rows are the ones written BEFORE an irreversible step (submit, pay): if the
        ledger cannot take them, the step does not happen. All other rows are best effort so that a
        broken ledger can never stop a failure from being recorded or reported; the gap is written
        to the audit chain instead and can be backfilled.
        """
        if self.ledger is None:
            return
        try:
            self.ledger.append(
                txn_id=ctx.txn_id, stage=stage, outcome=outcome, state=ctx.state, batch_id=ctx.batch_id,
                name=ctx.name, code=code, actor=actor, detail=detail,
                amount_rm=str(ctx.calc.total_duty_rm) if ctx.calc else None,
            )
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise
            logging.getLogger("stsds.pipeline").error("ledger write failed for %s: %s", ctx.txn_id, type(exc).__name__)
            self.audit.append(ctx.txn_id, "LEDGER_WRITE_FAILED", {"stage": stage, "outcome": outcome, "error": type(exc).__name__})

    def _halt(self, ctx: TxnContext, exc: GateFailure | engine.NeedsReview, stage: str = "") -> StageResult:
        route = getattr(exc, "route", "REVIEW")
        ctx.state = "HALTED" if route == "HALT" else "REVIEW"
        ctx.last_halt = {"code": exc.code, "message": exc.message, "route": route}
        ctx.review = (
            ReviewInfo(stage=stage, code=exc.code, message=exc.message, resolvable=exc.code in RESOLVABLE_CODES)
            if ctx.state == "REVIEW" else None
        )
        self.store.save(ctx)
        self.audit.append(ctx.txn_id, "HALT", {"code": exc.code, "message": exc.message, "route": route})
        self._log(ctx, stage or "pipeline", route, code=exc.code, detail=exc.message)
        self.notifier.alert(ctx.txn_id, exc.code, exc.message, route)
        return StageResult(
            ok=False, txn_id=ctx.txn_id, state=ctx.state,
            halt={"code": exc.code, "message": exc.message, "route": route},
        )

    def _hold(self, ctx: TxnContext, exc: GateFailure, state: str, stage: str) -> StageResult:
        """Park a transaction in an explicit waiting state (not a generic halt) and alert."""
        ctx.state = state
        ctx.last_halt = {"code": exc.code, "message": exc.message, "route": exc.route}
        self.store.save(ctx)
        self.audit.append(ctx.txn_id, "HOLD", {"state": state, "code": exc.code, "message": exc.message})
        self._log(ctx, stage, "HOLD", code=exc.code, detail=exc.message)
        self.notifier.alert(ctx.txn_id, exc.code, exc.message, exc.route)
        return StageResult(
            ok=False, txn_id=ctx.txn_id, state=state,
            halt={"code": exc.code, "message": exc.message, "route": exc.route},
        )

    def _ok(self, ctx: TxnContext, event: str, data: dict[str, Any], actor: str = "system") -> StageResult:
        self.store.save(ctx)
        self.audit.append(ctx.txn_id, event, data)
        self._log(ctx, event, "OK", actor=actor)
        return StageResult(ok=True, txn_id=ctx.txn_id, state=ctx.state, data=data)

    def _ctx(self, txn_id: str) -> TxnContext:
        ctx = self.store.load(txn_id)
        if ctx is None:
            raise GateFailure("TXN_UNKNOWN", "Unknown transaction", "HALT")
        return ctx

    def fail_internal(self, txn_id: str, stage: str, exc: Exception) -> StageResult:
        """An unexpected exception must never leave a transaction in limbo or retry blindly."""
        ctx = self._ctx(txn_id)
        reason = GateFailure("INTERNAL_ERROR", f"Unexpected {type(exc).__name__} during {stage}", "HALT")
        uncertain = {"PAYING": "PAYMENT_UNCERTAIN", "SUBMITTING": "SUBMIT_UNCERTAIN"}
        if ctx.state in uncertain:
            reason = GateFailure(
                "INTERNAL_ERROR_UNCERTAIN",
                f"Unexpected {type(exc).__name__} after an irreversible step started; reconcile before any retry",
                "HALT",
            )
            return self._hold(ctx, reason, uncertain[ctx.state], stage)
        return self._halt(ctx, reason, stage)

    # -- node 1/2: ingest ----------------------------------------------------------------------
    def ingest(
        self, pdf_b64: str, source: str, received_in_malaysia: dt.date | None = None,
        *, batch_id: str | None = None, name: str = "",
    ) -> StageResult:
        try:
            pdf = base64.b64decode(pdf_b64, validate=True)
        except ValueError as exc:
            raise GateFailure("PDF_BASE64", "Document is not valid base64", "HALT") from exc
        return self.ingest_bytes(pdf, source, received_in_malaysia, batch_id=batch_id, name=name)

    def ingest_bytes(
        self, pdf: bytes, source: str, received_in_malaysia: dt.date | None = None,
        *, batch_id: str | None = None, name: str = "",
    ) -> StageResult:
        pages = check_pdf(pdf)
        sha = hashlib.sha256(pdf).hexdigest()
        txn_id = sha[:32]
        existing = self.store.load(txn_id)
        if existing is not None:  # idempotency: the same document is never filed twice
            return StageResult(ok=True, txn_id=txn_id, state=existing.state, data={"duplicate": True})
        clean_name = "".join(ch for ch in name if ch.isprintable())[:80]
        ctx = TxnContext(
            txn_id=txn_id, doc_sha256=sha, source=source[:80], received_in_malaysia=received_in_malaysia,
            batch_id=batch_id, name=clean_name,
        )
        self.store.write(txn_id, ".pdf", pdf)
        try:
            text = self.extractor.extract(pdf)
            self.store.write(txn_id, ".txt", text.encode())
        except GateFailure as exc:
            return self._halt(ctx, exc, "ingest")
        try:
            report = signature_gate(pdf, self.s.min_signatures)
        except GateFailure as exc:
            return self._halt(ctx, exc, "ingest")
        ctx.signatures = report.covering
        return self._ok(
            ctx, "INGESTED",
            {"sha256": sha, "pages": pages, "source": ctx.source, "signatures": report.covering},
        )

    # -- node 3: classify, validate, score ----------------------------------------------------
    def classify(self, txn_id: str) -> StageResult:
        ctx = self._ctx(txn_id)
        text = (self.store.read(txn_id, ".txt") or b"").decode()
        try:
            c = self.classifier.classify(text)
            ctx.candidate = c  # kept so a reviewer can see exactly what the model extracted
            clock = sec47_clock(c.execution_date, self._today(), ctx.received_in_malaysia)
            ctx.assessment = assess(c, text, clock, self.rules, self.rules_sha, self.s.auto_threshold)
            confidence_gate(c)
            grounding_gate(c, text)
            ctx.classification, ctx.clock, ctx.state = c, clock, "CLASSIFIED"
            clock_gate(clock)
            # Completeness/JPPH problems are reported by their own stage with a precise code.
            if ctx.assessment.decision == "REVIEW" and ctx.assessment.components["completeness"] == 1.0:
                raise GateFailure(
                    "LOW_SCORE",
                    f"Document score {ctx.assessment.score:.2f} is below the auto-process threshold "
                    f"{self.s.auto_threshold:.2f}: " + "; ".join(ctx.assessment.reasons),
                )
            self._claim_instrument(ctx, c)
        except GateFailure as exc:
            return self._halt(ctx, exc, "classify")
        return self._ok(
            ctx, "CLASSIFIED",
            {
                "item": c.first_schedule_item.value, "confidence": c.confidence,
                "score": ctx.assessment.score, "decision": ctx.assessment.decision,
                "clock": clock.model_dump(mode="json"),
            },
        )

    def _claim_instrument(self, ctx: TxnContext, c: Classification) -> None:
        owner = self.store.claim_fingerprint(instrument_fingerprint(c), ctx.txn_id)
        if owner != ctx.txn_id:
            raise GateFailure(
                "DUPLICATE_INSTRUMENT",
                f"The same instrument (type, date, figures and parties) is already being handled as {owner[:8]}",
                "REVIEW",
            )

    # -- human-in-the-loop ---------------------------------------------------------------------
    def review_resolve(
        self, txn_id: str, reviewer: str, decision: Literal["approve", "reject"],
        *, corrections: Classification | None = None, attest: bool = False, note: str = "",
    ) -> StageResult:
        """A named person checks the document and either releases it or closes it.

        Approve never bypasses a hard gate: the engine, party checks, JPPH, phase, parity, signed
        rules and live mode all still apply afterwards. What it replaces is the model's confidence
        and the verbatim-grounding check, and only on the reviewer's explicit attestation.
        """
        ctx = self._ctx(txn_id)
        who = reviewer.strip()[:80]
        if ctx.state != "REVIEW" or ctx.review is None:
            raise GateFailure("NOT_IN_REVIEW", "This document is not waiting for review", "HALT")
        if not who:
            raise GateFailure("REVIEWER_REQUIRED", "A named reviewer is required", "HALT")
        if decision == "reject":
            ctx.state, ctx.reviewed_by = "REJECTED", who
            self.store.save(ctx)
            self.audit.append(txn_id, "REVIEW_REJECTED", {"reviewer": who, "note": note[:300], "code": ctx.review.code})
            self._log(ctx, "review", "REJECTED", code=ctx.review.code, detail=note, actor=who)
            return StageResult(ok=True, txn_id=txn_id, state=ctx.state, data={"reviewer": who})
        if not ctx.review.resolvable:
            raise GateFailure(
                "NOT_RESOLVABLE",
                f"{ctx.review.code} cannot be cleared by editing the extraction; it can only be rejected",
                "HALT",
            )
        if not attest:
            raise GateFailure(
                "ATTESTATION_REQUIRED",
                "Approving requires attesting that every figure, date and party was checked against the document",
                "HALT",
            )
        base = corrections or ctx.candidate or ctx.classification
        if base is None:
            raise GateFailure("NO_EXTRACTION", "There is no extraction to approve; supply corrections", "HALT")
        changes = classification_changes(ctx.candidate or ctx.classification, base)
        text = (self.store.read(txn_id, ".txt") or b"").decode()
        clock = sec47_clock(base.execution_date, self._today(), ctx.received_in_malaysia)
        try:
            clock_gate(clock)  # a human cannot approve a document past its statutory window
        except GateFailure as exc:
            return self._halt(ctx, exc, "review")
        try:
            self._claim_instrument(ctx, base)  # a reviewer cannot release a second copy of a filed instrument
        except GateFailure as exc:
            raise GateFailure(exc.code, exc.message + "; reject this copy", "HALT") from exc
        ungrounded, _ = grounding_report(base, text)
        ctx.classification, ctx.clock = base, clock
        ctx.reviewed_by, ctx.human_verified, ctx.review = who, True, None
        ctx.needs_second_approval = ctx.needs_second_approval or bool(changes)
        ctx.calc, ctx.draft_id, ctx.lhdn_total = None, None, None  # recompute everything downstream
        ctx.state = "CLASSIFIED"
        return self._ok(
            ctx, "REVIEW_APPROVED",
            {
                "reviewer": who, "note": note[:300], "changed_fields": changes,
                "not_found_in_text_after_review": ungrounded, "second_approval_required": ctx.needs_second_approval,
            },
            actor=who,
        )

    def work_items(self) -> list[dict[str, Any]]:
        """Everything waiting on a person: reviews, approvals, uncertain money movements."""
        waiting = {"REVIEW", "AWAITING_APPROVAL", "PAYMENT_UNCERTAIN", "SUBMIT_UNCERTAIN", "VERIFY_FAILED"}
        items: list[dict[str, Any]] = []
        for txn_id in self.store.list_ids():
            ctx = self.store.load(txn_id)
            if ctx is None or ctx.state not in waiting:
                continue
            halt = ctx.last_halt or {}
            items.append({
                "txn_id": txn_id, "state": ctx.state, "batch_id": ctx.batch_id, "name": ctx.name,
                "code": (ctx.review.code if ctx.review else halt.get("code", "")),
                "message": (ctx.review.message if ctx.review else halt.get("message", "")),
                "resolvable": bool(ctx.review and ctx.review.resolvable),
                "score": ctx.assessment.score if ctx.assessment else None,
                "duty_rm": str(ctx.calc.total_duty_rm) if ctx.calc else None,
                "needs_second_approval": ctx.needs_second_approval, "reviewed_by": ctx.reviewed_by,
            })
        items.sort(key=lambda i: (i["score"] is None, i["score"] or 0.0))
        return items

    def review_detail(self, txn_id: str) -> dict[str, Any]:
        ctx = self._ctx(txn_id)
        text = (self.store.read(txn_id, ".txt") or b"").decode()
        extraction = ctx.candidate or ctx.classification
        return {
            "txn_id": txn_id, "state": ctx.state, "name": ctx.name, "batch_id": ctx.batch_id,
            "review": ctx.review.model_dump() if ctx.review else None,
            "assessment": ctx.assessment.model_dump() if ctx.assessment else None,
            "extraction": json.loads(extraction.model_dump_json()) if extraction else None,
            "text": text[:20000], "text_truncated": len(text) > 20000,
            "duty_rm": str(ctx.calc.total_duty_rm) if ctx.calc else None,
            "reviewed_by": ctx.reviewed_by, "needs_second_approval": ctx.needs_second_approval,
        }

    # -- node 4 --------------------------------------------------------------------------------
    def calculate(self, txn_id: str) -> StageResult:
        ctx = self._ctx(txn_id)
        if ctx.classification is None:
            return self._halt(ctx, GateFailure("STAGE_ORDER", "Classification missing", "HALT"), "calculate")
        try:
            calc = engine.calculate(ctx.classification, self.rules, self.rules_sha)
        except engine.NeedsReview as exc:
            return self._halt(ctx, exc, "calculate")
        ctx.calc, ctx.state = calc, "CALCULATED"
        return self._ok(ctx, "CALCULATED", calc.model_dump(mode="json"))

    # -- node 5 --------------------------------------------------------------------------------
    def preflight(self, txn_id: str) -> StageResult:
        ctx = self._ctx(txn_id)
        c = ctx.classification
        if c is None or ctx.calc is None:
            return self._halt(ctx, GateFailure("STAGE_ORDER", "Calculation missing", "HALT"), "preflight")
        try:
            jpph_gate(c)
            phase_gate(c.first_schedule_item, c.execution_date, self._today(), self.rules, self.s.phase2_enabled)
            preflight_parties(c.parties, self.verifier)
        except GateFailure as exc:
            return self._halt(ctx, exc, "preflight")
        ctx.state = "PREFLIGHT_OK"
        return self._ok(ctx, "PREFLIGHT_OK", {"parties": [mask(p.tin) for p in c.parties]})

    # -- node 6/7: draft + parity gate ----------------------------------------------------------
    async def draft(self, txn_id: str) -> StageResult:
        ctx = self._ctx(txn_id)
        if ctx.state != "PREFLIGHT_OK" or ctx.classification is None or ctx.calc is None:
            return self._halt(ctx, GateFailure("STAGE_ORDER", "Preflight has not passed", "HALT"), "draft")
        fields, parties = bnds_fields(ctx.classification)
        try:
            result = await self.rpa.draft(ctx.classification.bnds_module.value, fields, parties)
            ctx.draft_id, ctx.lhdn_total = result.draft_id, result.lhdn_total
            parity_gate(ctx.calc.total_duty_rm, result.lhdn_total)
        except GateFailure as exc:
            self.store.save(ctx)
            return self._halt(ctx, exc, "draft")
        ctx.state = "PARITY_OK"
        return self._ok(
            ctx, "PARITY_OK",
            {"engine_total": str(ctx.calc.total_duty_rm), "lhdn_total": str(ctx.lhdn_total), "draft_id": ctx.draft_id},
        )

    # -- node 8a: submit ------------------------------------------------------------------------
    async def submit(self, txn_id: str, approved_by: str | None = None) -> StageResult:
        ctx = self._ctx(txn_id)
        try:
            if ctx.state not in {"PARITY_OK", "AWAITING_APPROVAL"} or ctx.calc is None or ctx.draft_id is None:
                raise GateFailure("STAGE_ORDER", "Parity has not passed", "HALT")
            if self.s.mode != "live":
                raise GateFailure("SHADOW_MODE", "Worker is in shadow mode: draft only", "REVIEW")
            if self.rules.signoff is None:
                raise GateFailure("RULES_UNSIGNED", "Rule set has no Head of Tax sign-off", "HALT")
            needs_checker = ctx.calc.total_duty_rm >= self.s.maker_checker_threshold_rm or ctx.needs_second_approval
            if needs_checker:
                checker = (approved_by or "").strip()
                if not checker:
                    ctx.state = "AWAITING_APPROVAL"
                    self.store.save(ctx)
                    self.audit.append(txn_id, "AWAITING_APPROVAL", {
                        "total": str(ctx.calc.total_duty_rm), "human_edited": ctx.needs_second_approval})
                    self._log(ctx, "submit", "AWAITING_APPROVAL", code="MAKER_CHECKER")
                    return StageResult(
                        ok=False, txn_id=txn_id, state=ctx.state,
                        halt={"code": "MAKER_CHECKER", "message": "Checker approval required", "route": "APPROVAL"},
                    )
                if ctx.reviewed_by and checker.casefold() == ctx.reviewed_by.casefold():
                    ctx.state = "AWAITING_APPROVAL"
                    self.store.save(ctx)
                    self.audit.append(txn_id, "FOUR_EYES_REFUSED", {"person": checker})
                    self._log(ctx, "submit", "REFUSED", code="FOUR_EYES", actor=checker)
                    return StageResult(
                        ok=False, txn_id=txn_id, state=ctx.state,
                        halt={"code": "FOUR_EYES", "message": "The checker must differ from the reviewer", "route": "APPROVAL"},
                    )
                ctx.approved_by = checker[:80]
            # Log first (if the ledger is unwritable we stop here, before anything irreversible),
            # then write the marker: if we crash after the click, the state says so.
            self._log(ctx, "submit", "START", actor=ctx.approved_by or "system", strict=True)
            ctx.state = "SUBMITTING"
            self.store.save(ctx)
            self.audit.append(txn_id, "SUBMITTING", {"draft_id": ctx.draft_id, "approved_by": ctx.approved_by})
            try:
                result = await self.rpa.submit(ctx.draft_id)
            except GateFailure as exc:
                exc.message += " Check MyTax for an existing bill before any retry."
                raise
        except GateFailure as exc:
            return self._halt(ctx, exc, "submit")
        ctx.bill_no, ctx.virtual_account, ctx.state = result.bill_no, result.virtual_account, "SUBMITTED"
        return self._ok(
            ctx, "SUBMITTED",
            {"bill_no": result.bill_no, "virtual_account": result.virtual_account, "approved_by": ctx.approved_by},
            actor=ctx.approved_by or "system",
        )

    # -- payment --------------------------------------------------------------------------------
    async def pay(self, txn_id: str, approved_by: str | None = None) -> StageResult:
        ctx = self._ctx(txn_id)
        try:
            if self.payments is None:
                raise GateFailure("PAYMENT_UNCONFIGURED", "No payment provider is configured", "HALT")
            if ctx.state != "SUBMITTED" or ctx.calc is None or not ctx.bill_no:
                raise GateFailure("STAGE_ORDER", "There is no submitted bill to pay", "HALT")
            amount = ctx.calc.total_duty_rm
            if amount == 0:  # exempt instrument: nothing is owed, so nothing is sent to the provider
                ctx.payment = {"reference": "", "amount_rm": "0.00", "source": "no-payment-due"}
                ctx.state = "PAID"
                return self._ok(ctx, "PAID", dict(ctx.payment))
            known = await self.payments.status(ctx.bill_no)  # never pay a bill that is already paid
            if known.payment == "UNKNOWN":
                raise GateFailure("PAYMENT_UNCERTAIN", "Payment status of this bill is unclear", "HALT")
            if known.payment == "PAID":
                if known.amount_rm is not None and known.amount_rm != amount:
                    raise GateFailure("PAYMENT_AMOUNT", "Bill is already paid for a different amount", "HALT")
                ctx.payment = {"reference": known.reference or "", "amount_rm": str(amount), "source": "already-paid"}
                ctx.state = "PAID"
                return self._ok(ctx, "PAID", {**ctx.payment, "already_paid": True})
            if ctx.payment_attempts >= 1 and not (approved_by or "").strip():
                raise GateFailure("PAYMENT_RETRY_NEEDS_APPROVAL", "A second payment attempt needs a named approver", "HALT")
            self._log(ctx, "pay", "START", actor=(approved_by or "system"), strict=True)  # before the marker
            ctx.payment_attempts += 1
            ctx.state = "PAYING"  # write-ahead marker
            self.store.save(ctx)
            self.audit.append(ctx.txn_id, "PAYING", {"bill_no": ctx.bill_no, "amount_rm": str(amount), "attempt": ctx.payment_attempts})
            result = await self.payments.pay(
                idempotency_key=f"stsds-{ctx.txn_id}", bill_no=ctx.bill_no,
                virtual_account=ctx.virtual_account, amount_rm=amount,
            )
            if result.status != "PAID" or result.amount_rm != amount:
                raise GateFailure("PAYMENT_UNCERTAIN", "Provider reply does not confirm the exact amount", "HALT")
            confirm = await self.payments.status(ctx.bill_no)  # independent second read
            if confirm.payment != "PAID" or (confirm.amount_rm is not None and confirm.amount_rm != amount):
                raise GateFailure("PAYMENT_UNCERTAIN", "Payment could not be independently confirmed", "HALT")
        except GateFailure as exc:
            if ctx.state == "PAYING" and exc.code == "PAYMENT_REJECTED":
                ctx.state = "SUBMITTED"  # definite refusal: no money moved
                return self._hold(ctx, exc, "SUBMITTED", "pay")
            if ctx.state == "PAYING" or exc.code == "PAYMENT_UNCERTAIN":
                return self._hold(ctx, exc, "PAYMENT_UNCERTAIN", "pay")
            if ctx.state == "SUBMITTED":
                return self._hold(ctx, exc, "SUBMITTED", "pay")
            return self._halt(ctx, exc, "pay")
        ctx.payment = {"reference": result.reference, "amount_rm": str(amount), "source": "paid-now"}
        ctx.state = "PAID"
        return self._ok(ctx, "PAID", dict(ctx.payment), actor=(approved_by or "system"))

    async def reconcile_payment(self, txn_id: str, actor: str) -> StageResult:
        """Resolve PAYMENT_UNCERTAIN by asking the provider what actually happened."""
        ctx = self._ctx(txn_id)
        if ctx.state != "PAYMENT_UNCERTAIN" or self.payments is None or not ctx.bill_no or ctx.calc is None:
            raise GateFailure("NOT_UNCERTAIN", "This transaction has no uncertain payment", "HALT")
        known = await self.payments.status(ctx.bill_no)
        amount = ctx.calc.total_duty_rm
        if known.payment == "PAID" and known.amount_rm in (None, amount):
            ctx.payment = {"reference": known.reference or "", "amount_rm": str(amount), "source": f"reconciled-by-{actor}"}
            ctx.state = "PAID"
            return self._ok(ctx, "PAYMENT_RECONCILED_PAID", dict(ctx.payment), actor=actor)
        if known.payment == "UNPAID":
            ctx.state = "SUBMITTED"  # retry is allowed, but only with a named approver
            return self._ok(ctx, "PAYMENT_RECONCILED_UNPAID", {"retry_needs_approver": True}, actor=actor)
        return self._hold(
            ctx, GateFailure("PAYMENT_UNCERTAIN", "Provider still cannot confirm the payment", "HALT"),
            "PAYMENT_UNCERTAIN", "reconcile",
        )

    # -- node 8b: capture the one-time certificate and vault -----------------------------------
    async def complete(self, txn_id: str) -> StageResult:
        ctx = self._ctx(txn_id)
        try:
            ready = {"PAID"} if self.payments is not None else {"SUBMITTED", "PAID"}
            if ctx.state not in ready or ctx.draft_id is None or ctx.calc is None or ctx.lhdn_total is None:
                raise GateFailure("STAGE_ORDER", "Submission (and payment) has not completed", "HALT")
            contract = self.store.read(txn_id, ".pdf")
            if contract is None:
                raise GateFailure("CONTRACT_MISSING", "Staged contract not found", "HALT")
            sijil = await self.rpa.download_sijil(ctx.draft_id)
            ctx.qr = self._read_qr(sijil)  # never allowed to stop the vaulting of the one-time download
            record = {
                "txn_id": txn_id, "bill_no": ctx.bill_no, "draft_id": ctx.draft_id,
                "classification": json.loads(ctx.classification.model_dump_json()) if ctx.classification else None,
                "calculation": json.loads(ctx.calc.model_dump_json()),
                "lhdn_total": str(ctx.lhdn_total), "approved_by": ctx.approved_by,
                "reviewed_by": ctx.reviewed_by, "payment": ctx.payment,
                "assessment": json.loads(ctx.assessment.model_dump_json()) if ctx.assessment else None,
                "qr": ctx.qr, "signatures": ctx.signatures,
            }
            receipt = self.vault.archive(txn_id, contract, record, ctx.calc, str(ctx.lhdn_total), sijil)
        except GateFailure as exc:
            return self._halt(ctx, exc, "complete")
        ctx.vault, ctx.state, ctx.stamped_on = receipt.__dict__.copy(), "VAULTED", self._today()
        self.store.delete_pdf(txn_id)
        return self._ok(ctx, "VAULTED", receipt.__dict__.copy())

    @staticmethod
    def _read_qr(sijil: bytes) -> dict[str, str]:
        """Decode the certificate's QR code. Failure is recorded, not raised: the vault comes first."""
        try:
            urls = read_qr_urls(sijil)
        except Exception as exc:  # noqa: BLE001 - a hostile or odd PDF must not lose the one-time file
            return {"found": "false", "error": type(exc).__name__}
        if not urls:
            return {"found": "false"}
        return {"found": "true", "url": urls[0][:2048], "count": str(len(urls))}

    async def _qr_check(self, ctx: TxnContext) -> tuple[bool | None, str]:
        """Return (verdict, reason). None means the portal could not answer (not calibrated yet)."""
        hosts = parse_hosts(self.s.qr_verify_hosts)
        if not hosts:
            return None, "no QR host configured"
        if not ctx.qr or ctx.qr.get("found") != "true":
            return False, "no QR code found on the certificate"
        if ctx.qr.get("count") != "1":
            return False, "certificate holds more than one QR code"
        try:
            check_qr_url(ctx.qr["url"], hosts)
        except GateFailure as exc:
            return False, exc.code
        opener = getattr(self.rpa, "verify_qr", None)
        if opener is None:
            return None, "portal port cannot verify QR"
        try:
            result = await opener(ctx.qr["url"])
        except GateFailure as exc:
            return False, exc.code
        if result.valid is None:
            return None, "verification page not readable"
        if not result.valid:
            return False, "portal says the certificate is not valid"
        if result.amount_rm is not None and ctx.calc is not None and result.amount_rm != ctx.calc.total_duty_rm:
            return False, "amount on the verification page differs from the duty"
        return True, ""

    # -- final verification ----------------------------------------------------------------------
    async def verify(self, txn_id: str) -> StageResult:
        """Independent end-of-process checks. COMPLETE is only reachable through here."""
        ctx = self._ctx(txn_id)
        if ctx.state != "VAULTED" or ctx.calc is None or ctx.vault is None:
            return self._halt(ctx, GateFailure("STAGE_ORDER", "Nothing to verify yet", "HALT"), "verify")
        checks: dict[str, str] = {}
        failures: list[str] = []

        def record(name: str, ok: bool | None, why: str = "") -> None:
            checks[name] = "PASS" if ok else ("SKIPPED" if ok is None else f"FAIL: {why}")
            if ok is False:
                failures.append(name)

        record("audit_chain", verify_chain(self.audit.records(txn_id)), "hash chain broken")
        record("certificate_hashed", bool(ctx.vault.get("sijil_sha256")) and bool(ctx.vault.get("bundle_sha256")), "no hash")
        record("vault_retention", bool(ctx.vault.get("retain_until")), "no retention date")
        record("amounts_agree", ctx.lhdn_total == ctx.calc.total_duty_rm, "engine and LHDN totals differ")
        if self.payments is not None:
            paid_ok = bool(ctx.payment) and ctx.payment is not None and ctx.payment.get("amount_rm") == str(ctx.calc.total_duty_rm)
            record("payment_matches_duty", paid_ok, "recorded payment differs from duty")
            if ctx.bill_no and ctx.calc.total_duty_rm > 0:
                try:
                    st = await self.payments.status(ctx.bill_no)
                    record("payment_confirmed_by_provider", st.payment == "PAID", f"provider says {st.payment}")
                except GateFailure as exc:
                    record("payment_confirmed_by_provider", False, exc.code)
        else:
            record("payment_matches_duty", None)
        portal_check = getattr(self.rpa, "bill_status", None)
        if portal_check is not None and ctx.bill_no:
            try:
                status = await portal_check(ctx.bill_no)
                record("portal_shows_stamped", status.stamped, "portal does not show the bill as stamped")
            except GateFailure as exc:
                record("portal_shows_stamped", False, exc.code)
        else:
            record("portal_shows_stamped", None)
        verdict, why = await self._qr_check(ctx)
        record("qr_verified", verdict, why)
        if self.ledger is not None:
            record("ledger_chain", self.ledger.verify(), "ledger hash chain broken")
        ctx.verification = checks
        if failures:
            return self._hold(
                ctx, GateFailure("VERIFY_FAILED", "Failed checks: " + ", ".join(failures), "HALT"), "VERIFY_FAILED", "verify"
            )
        ctx.state = "COMPLETE"
        return self._ok(ctx, "COMPLETE", {"checks": checks})

    # -- composite for shadow runs and tests ----------------------------------------------------
    async def run_to_parity(self, pdf_b64: str, source: str) -> StageResult:
        res = self.ingest(pdf_b64, source)
        for stage in (self.classify, self.calculate, self.preflight):
            if not res.ok:
                return res
            res = stage(res.txn_id)
        if not res.ok:
            return res
        return await self.draft(res.txn_id)

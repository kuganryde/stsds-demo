"""Batch intake: many documents in one call, each tracked independently.

One bad document never stops the others. Each document gets its own transaction id (SHA-256 of the
file), its own audit chain and its own ledger rows. The batch record only groups them, enforces
size limits, applies a payment cap across the batch and produces the end-of-run report.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import tempfile
import threading
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .gates import GateFailure
from .orchestrator import Orchestrator
from .pipeline import TxnContext
from .security import valid_txn_id

WAITING = frozenset({"REVIEW", "AWAITING_APPROVAL", "PAYMENT_UNCERTAIN", "SUBMIT_UNCERTAIN", "VERIFY_FAILED"})
FAILED = frozenset({"HALTED", "REJECTED"})


class BatchEntry(BaseModel):
    name: str
    txn_id: str | None = None
    duplicate: bool = False
    rejected: dict[str, str] | None = None


class Batch(BaseModel):
    batch_id: str
    created_at: str
    created_by: str
    source: str
    status: str = "CREATED"
    entries: list[BatchEntry] = []
    reserved: dict[str, str] = {}


def _clean(name: str) -> str:
    return "".join(ch for ch in name if ch.isprintable())[:80] or "document"


class BatchService:
    def __init__(self, orchestrator: Orchestrator) -> None:
        self.orch, self.p = orchestrator, orchestrator.p
        self.root = Path(self.p.store.root) / "batches"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._mutex = threading.Lock()
        orchestrator.before_submit = self._reserve

    # -- persistence -----------------------------------------------------------------------------
    def _path(self, batch_id: str) -> Path:
        if not valid_txn_id(batch_id):
            raise GateFailure("BATCH_ID", "Invalid batch id", "HALT")
        return self.root / f"{batch_id}.json"

    def _save(self, batch: Batch) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.root)
        with os.fdopen(fd, "wb") as fh:
            fh.write(batch.model_dump_json().encode())
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path(batch.batch_id))

    def get(self, batch_id: str) -> Batch:
        path = self._path(batch_id)
        if not path.exists():
            raise GateFailure("BATCH_UNKNOWN", "Unknown batch", "HALT")
        return Batch.model_validate_json(path.read_text())

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        """Newest batches first, as short summaries (no per-document rows)."""
        files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        out: list[dict[str, Any]] = []
        for path in files:
            try:
                s = self.summary(path.stem)
            except (GateFailure, ValueError):
                continue  # a damaged file must not hide the others
            out.append({k: s[k] for k in ("batch_id", "status", "created_at", "created_by", "counts", "totals")})
        return out

    # -- intake ----------------------------------------------------------------------------------
    async def create(
        self, created_by: str, source: str, documents: list[tuple[str, bytes | None]],
        received_in_malaysia: dt.date | None = None,
    ) -> Batch:
        limit = self.p.s.batch_max_docs
        if not documents:
            raise GateFailure("BATCH_EMPTY", "A batch needs at least one document", "HALT")
        if len(documents) > limit:
            raise GateFailure("BATCH_TOO_LARGE", f"A batch is limited to {limit} documents", "HALT")
        batch = Batch(
            batch_id=uuid.uuid4().hex, created_at=dt.datetime.now(dt.UTC).isoformat(),
            created_by=created_by[:80], source=source[:80],
        )
        seen: set[str] = set()
        for raw_name, pdf in documents:
            name = _clean(raw_name)
            txn_id = hashlib.sha256(pdf if pdf is not None else name.encode()).hexdigest()[:32]
            if txn_id in seen:
                batch.entries.append(BatchEntry(name=name, txn_id=txn_id, duplicate=True))
                continue
            seen.add(txn_id)
            try:
                if pdf is None:
                    raise GateFailure("PDF_BASE64", "Document is not valid base64", "HALT")
                res = await asyncio.to_thread(
                    self.p.ingest_bytes, pdf, source, received_in_malaysia, batch_id=batch.batch_id, name=name
                )
            except GateFailure as exc:  # invalid PDF: recorded, never stops the rest of the batch
                self.p.audit.append(txn_id, "REJECTED_AT_INTAKE", {"code": exc.code, "message": exc.message})
                if self.p.ledger is not None:
                    self.p.ledger.append(
                        txn_id=txn_id, stage="intake", outcome="REJECTED", state="REJECTED",
                        batch_id=batch.batch_id, name=name, code=exc.code, detail=exc.message, actor=created_by,
                    )
                batch.entries.append(BatchEntry(name=name, rejected={"code": exc.code, "message": exc.message}))
                continue
            batch.entries.append(
                BatchEntry(name=name, txn_id=res.txn_id, duplicate=bool(res.data.get("duplicate")))
            )
        self._save(batch)
        return batch

    # -- batch payment cap -----------------------------------------------------------------------
    def _reserve(self, ctx: TxnContext) -> bool:
        """Count a filing against the batch cap before Submit. Idempotent per transaction."""
        if not ctx.batch_id or ctx.calc is None:
            return True
        with self._mutex:
            batch = self.get(ctx.batch_id)
            if ctx.txn_id in batch.reserved:
                return True
            committed = sum((Decimal(v) for v in batch.reserved.values()), Decimal("0"))
            if committed + ctx.calc.total_duty_rm > self.p.s.batch_payment_cap_rm:
                return False
            batch.reserved[ctx.txn_id] = str(ctx.calc.total_duty_rm)
            self._save(batch)
            return True

    # -- processing ------------------------------------------------------------------------------
    async def run(self, batch_id: str) -> dict[str, Any]:
        batch = self.get(batch_id)
        batch.status = "RUNNING"
        self._save(batch)
        ids = list(dict.fromkeys(e.txn_id for e in batch.entries if e.txn_id))
        await asyncio.gather(*(self.orch.advance(t) for t in ids), return_exceptions=True)
        batch = self.get(batch_id)
        batch.status = "DONE"
        self._save(batch)
        return self.summary(batch_id)

    # -- reporting -------------------------------------------------------------------------------
    def summary(self, batch_id: str) -> dict[str, Any]:
        batch = self.get(batch_id)
        docs: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        completed_duty = Decimal("0")
        for e in batch.entries:
            if e.rejected:
                docs.append({"name": e.name, "txn_id": None, "state": "REJECTED_AT_INTAKE", **{
                    "code": e.rejected["code"], "message": e.rejected["message"]}})
                counts["REJECTED_AT_INTAKE"] = counts.get("REJECTED_AT_INTAKE", 0) + 1
                continue
            ctx = self.p.store.load(e.txn_id or "")
            if ctx is None:
                continue
            halt = ctx.last_halt or {}
            extraction = ctx.classification or ctx.candidate
            docs.append({
                "name": e.name, "txn_id": ctx.txn_id, "state": ctx.state, "duplicate_in_batch": e.duplicate,
                "item": extraction.first_schedule_item.value if extraction else None,
                "score": ctx.assessment.score if ctx.assessment else None,
                "decision": ctx.assessment.decision if ctx.assessment else None,
                "duty_rm": str(ctx.calc.total_duty_rm) if ctx.calc else None,
                "reviewed_by": ctx.reviewed_by, "approved_by": ctx.approved_by,
                "bill_no": ctx.bill_no,
                "payment_reference": (ctx.payment or {}).get("reference"),
                "vault_key": (ctx.vault or {}).get("bundle_key"),
                "verification": ctx.verification,
                "code": halt.get("code") if ctx.state not in {"COMPLETE"} else None,
                "message": halt.get("message") if ctx.state not in {"COMPLETE"} else None,
            })
            if not e.duplicate:
                counts[ctx.state] = counts.get(ctx.state, 0) + 1
                if ctx.state == "COMPLETE" and ctx.calc:
                    completed_duty += ctx.calc.total_duty_rm
        unique = [d for d in docs if not d.get("duplicate_in_batch")]
        totals = {
            "documents": len(batch.entries),
            "unique_documents": len(unique),
            "complete": sum(1 for d in unique if d["state"] == "COMPLETE"),
            "waiting_for_people": sum(1 for d in unique if d["state"] in WAITING),
            "failed": sum(1 for d in unique if d["state"] in FAILED or d["state"] == "REJECTED_AT_INTAKE"),
            "in_progress": sum(
                1 for d in unique
                if d["state"] not in WAITING | FAILED | {"COMPLETE", "REJECTED_AT_INTAKE"}
            ),
            "duty_completed_rm": str(completed_duty),
            "payment_cap_rm": str(self.p.s.batch_payment_cap_rm),
        }
        return {
            "batch_id": batch.batch_id, "status": batch.status, "created_at": batch.created_at,
            "created_by": batch.created_by, "counts": counts, "totals": totals, "documents": docs,
        }

    def json_dump(self, batch_id: str) -> str:
        return json.dumps(self.summary(batch_id), indent=2)

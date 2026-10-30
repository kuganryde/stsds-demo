"""HTTP surface used by n8n (internal Docker network only). One endpoint per orchestration node.

Auth: /v1/ingest verifies the upstream webhook HMAC; every other /v1 route requires the service
bearer token. Bodies are size-capped. Responses to stage calls are always HTTP 200 with ``ok`` and
``halt`` so n8n IF nodes can branch; malformed or unauthorised calls return 4xx.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import datetime as dt
import io
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ValidationError

from . import __version__, engine
from .audit import verify_chain
from .batch import BatchService
from .clients import ClientDirectory, ClientIn, ClientPatch, SuspendIn, pending_from_holds
from .gates import GateFailure
from .ledger import Ledger, csv_safe
from .orchestrator import Orchestrator
from .pipeline import Pipeline, StageResult
from .register import MAX_IMPORT_BYTES, ManualIn, OverrideIn, Register
from .register import Entry as RegEntry
from .register import alerts as monitoring_alerts_fn
from .register import monthly_report as monthly_report_fn
from .register import summary as monitoring_summary_fn
from .register import to_csv as register_to_csv
from .register import unstamped as unstamped_fn
from .schemas import Classification
from .security import AuthError, valid_txn_id, verify_bearer, verify_webhook
from .settings import Settings
from .signing import SignatureProvider, Signer, SigningService
from .users import Identity, UserDirectory

log = logging.getLogger("stsds.api")


class IngestBody(BaseModel):
    source: str
    document_id: str
    pdf_base64: str
    received_in_malaysia: dt.date | None = None


class SubmitBody(BaseModel):
    approved_by: str | None = None


class BatchDoc(BaseModel):
    name: str
    pdf_base64: str


class BatchBody(BaseModel):
    source: str = "batch-upload"
    received_in_malaysia: dt.date | None = None
    auto_run: bool = True
    documents: list[BatchDoc]


class DecisionBody(BaseModel):
    decision: Literal["approve", "reject"]
    attest: bool = False
    note: str = ""
    corrections: dict[str, Any] | None = None


class SignerBody(BaseModel):
    name: str
    email: str


class SigningBody(BaseModel):
    name: str
    pdf_base64: str
    signers: list[SignerBody]


class EnvelopeHook(BaseModel):
    envelope_id: str


class AlertBody(BaseModel):
    txn_id: str
    code: str
    message: str
    route: str = "HALT"


CONSOLE_DIR = Path(__file__).parent / "console"
CONSOLE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def create_app(
    pipeline: Pipeline, settings: Settings, users: UserDirectory | None = None,
    signer: SignatureProvider | None = None,
    register: Register | None = None,
) -> FastAPI:
    app = FastAPI(title="STSDS Engine", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    orchestrator = Orchestrator(pipeline)
    batches = BatchService(orchestrator)

    @app.middleware("http")
    async def limit_body(request: Request, call_next: Any) -> Any:
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.max_body_bytes:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    def principal(*roles: str) -> Callable[..., Identity]:
        """Resolve the caller to a named identity and require one of ``roles``.

        The shared service token (n8n) is an anonymous 'operator'. Reviewing and approving always
        need a named person from the user directory, so every human decision is attributable.
        """

        def dep(authorization: str | None = Header(default=None)) -> Identity:
            ident: Identity | None = None
            if users is not None:
                try:
                    ident = users.authenticate(authorization)
                except AuthError:
                    ident = None
            if ident is None:
                try:
                    verify_bearer(authorization, settings.engine_token)
                    ident = Identity("service", frozenset({"operator"}))
                except AuthError as exc:
                    raise HTTPException(401, "unauthorised") from exc
            if not any(ident.has(r) for r in roles):
                raise HTTPException(403, "your role cannot do this")
            return ident

        return dep

    def service_auth(authorization: str | None = Header(default=None)) -> None:
        try:
            verify_bearer(authorization, settings.engine_token)
        except AuthError as exc:
            raise HTTPException(401, "unauthorised") from exc

    def txn(txn_id: str) -> str:
        if not valid_txn_id(txn_id):
            raise HTTPException(400, "invalid transaction id")
        return txn_id

    def guarded(fn_result: StageResult) -> dict[str, Any]:
        return fn_result.model_dump(mode="json")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "mode": settings.mode, "rules": pipeline.rules.version}

    @app.post("/v1/ingest")
    async def ingest(
        request: Request,
        x_signature: str | None = Header(default=None),
        x_timestamp: str | None = Header(default=None),
    ) -> dict[str, Any]:
        raw = await request.body()
        try:
            verify_webhook(raw, x_signature, x_timestamp, settings.webhook_secret)
            body = IngestBody.model_validate(json.loads(raw))
        except AuthError as exc:
            raise HTTPException(401, "unauthorised") from exc
        except (ValueError, ValidationError) as exc:
            raise HTTPException(422, "invalid body") from exc
        try:
            return guarded(pipeline.ingest(body.pdf_base64, body.source, body.received_in_malaysia))
        except GateFailure as exc:
            return {"ok": False, "halt": {"code": exc.code, "message": exc.message, "route": exc.route}}

    @app.post("/v1/classify/{txn_id}", dependencies=[Depends(service_auth)])
    def classify(txn_id: str) -> dict[str, Any]:
        return guarded(pipeline.classify(txn(txn_id)))

    @app.post("/v1/calculate/{txn_id}", dependencies=[Depends(service_auth)])
    def calculate(txn_id: str) -> dict[str, Any]:
        return guarded(pipeline.calculate(txn(txn_id)))

    @app.post("/v1/preflight/{txn_id}", dependencies=[Depends(service_auth)])
    def preflight(txn_id: str) -> dict[str, Any]:
        return guarded(pipeline.preflight(txn(txn_id)))

    @app.post("/v1/rpa/draft/{txn_id}", dependencies=[Depends(service_auth)])
    async def draft(txn_id: str) -> dict[str, Any]:
        return guarded(await pipeline.draft(txn(txn_id)))

    @app.post("/v1/rpa/submit/{txn_id}", dependencies=[Depends(service_auth)])
    async def submit(txn_id: str, body: SubmitBody = Body(default_factory=SubmitBody)) -> dict[str, Any]:
        return guarded(await pipeline.submit(txn(txn_id), body.approved_by))

    @app.post("/v1/rpa/pay/{txn_id}", dependencies=[Depends(service_auth)])
    async def pay(txn_id: str) -> dict[str, Any]:
        return guarded(await pipeline.pay(txn(txn_id)))

    @app.post("/v1/rpa/verify/{txn_id}", dependencies=[Depends(service_auth)])
    async def verify(txn_id: str) -> dict[str, Any]:
        return guarded(await pipeline.verify(txn(txn_id)))

    @app.post("/v1/rpa/complete/{txn_id}", dependencies=[Depends(service_auth)])
    async def complete(txn_id: str) -> dict[str, Any]:
        return guarded(await pipeline.complete(txn(txn_id)))

    @app.post("/v1/calc", dependencies=[Depends(service_auth)])
    def calc(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Pure calculation, for conformance testing and what-if checks. No side effects."""
        try:
            c = Classification.model_validate(payload)
            return engine.calculate(c, pipeline.rules, pipeline.rules_sha).model_dump(mode="json")
        except ValidationError as exc:
            raise HTTPException(422, "invalid classification") from exc
        except engine.NeedsReview as exc:
            return {"needs_review": True, "code": exc.code, "message": exc.message}

    @app.post("/v1/alert", dependencies=[Depends(service_auth)])
    def alert(body: AlertBody) -> dict[str, bool]:
        pipeline.notifier.alert(txn(body.txn_id), body.code[:60], body.message[:300], body.route[:20])
        return {"sent": True}

    @app.get("/v1/audit/{txn_id}", dependencies=[Depends(service_auth)])
    def audit(txn_id: str) -> dict[str, Any]:
        records = pipeline.audit.records(txn(txn_id))
        return {
            "txn_id": txn_id,
            "chain_valid": verify_chain(records),
            "records": [r.__dict__ for r in records],
        }

    # ---- batches -----------------------------------------------------------------------------
    @app.post("/v1/batches")
    async def create_batch(
        body: BatchBody, background: BackgroundTasks, who: Identity = Depends(principal("operator"))
    ) -> dict[str, Any]:
        docs: list[tuple[str, bytes | None]] = []
        for d in body.documents:
            try:
                docs.append((d.name, base64.b64decode(d.pdf_base64, validate=True)))
            except (binascii.Error, ValueError):
                docs.append((d.name, None))
        try:
            batch = await batches.create(who.name, body.source, docs, body.received_in_malaysia)
        except GateFailure as exc:
            raise HTTPException(422, {"code": exc.code, "message": exc.message}) from exc
        if body.auto_run:
            background.add_task(batches.run, batch.batch_id)
        return {"batch_id": batch.batch_id, "accepted": sum(1 for e in batch.entries if e.txn_id),
                "rejected_at_intake": sum(1 for e in batch.entries if e.rejected), "auto_run": body.auto_run}

    @app.get("/v1/batches/{batch_id}")
    def batch_summary(
        batch_id: str, _who: Identity = Depends(principal("operator", "reviewer", "checker"))
    ) -> dict[str, Any]:
        try:
            return batches.summary(txn(batch_id))
        except GateFailure as exc:
            raise HTTPException(404, exc.message) from exc

    @app.post("/v1/batches/{batch_id}/run")
    async def batch_run(batch_id: str, _who: Identity = Depends(principal("operator"))) -> dict[str, Any]:
        try:
            return await batches.run(txn(batch_id))
        except GateFailure as exc:
            raise HTTPException(404, exc.message) from exc

    @app.get("/v1/batches/{batch_id}/report.csv")
    def batch_report(batch_id: str, _who: Identity = Depends(principal("operator", "checker"))) -> Response:
        try:
            summary = batches.summary(txn(batch_id))
        except GateFailure as exc:
            raise HTTPException(404, exc.message) from exc
        cols = ("name", "txn_id", "state", "item", "score", "decision", "duty_rm", "reviewed_by",
                "approved_by", "bill_no", "payment_reference", "vault_key", "code", "message")
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        for d in summary["documents"]:
            writer.writerow([csv_safe(d.get(c)) for c in cols])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv")

    @app.get("/v1/ledger")
    def ledger_rows(
        batch_id: str | None = None, txn_id: str | None = None,
        _who: Identity = Depends(principal("operator", "checker")),
    ) -> dict[str, Any]:
        if pipeline.ledger is None:
            raise HTTPException(503, "ledger not configured")
        if batch_id:
            txn(batch_id)
        if txn_id:
            txn(txn_id)
        return {"chain_valid": pipeline.ledger.verify(), "rows": pipeline.ledger.rows(batch_id=batch_id, txn_id=txn_id)}

    @app.get("/v1/ledger.csv")
    def ledger_csv(batch_id: str | None = None, _who: Identity = Depends(principal("operator", "checker"))) -> Response:
        if pipeline.ledger is None:
            raise HTTPException(503, "ledger not configured")
        if batch_id:
            txn(batch_id)
        return PlainTextResponse(Ledger.to_csv(pipeline.ledger.rows(batch_id=batch_id)), media_type="text/csv")

    # ---- human in the loop ---------------------------------------------------------------------
    @app.get("/v1/review/queue")
    def review_queue(_who: Identity = Depends(principal("reviewer", "checker"))) -> dict[str, Any]:
        return {"items": pipeline.work_items()}

    @app.get("/v1/review/{txn_id}")
    def review_detail(txn_id: str, _who: Identity = Depends(principal("reviewer", "checker"))) -> dict[str, Any]:
        try:
            return pipeline.review_detail(txn(txn_id))
        except GateFailure as exc:
            raise HTTPException(404, exc.message) from exc

    @app.get("/v1/review/{txn_id}/document")
    def review_document(txn_id: str, _who: Identity = Depends(principal("reviewer", "checker"))) -> Response:
        pdf = pipeline.store.read(txn(txn_id), ".pdf")
        if pdf is None:
            raise HTTPException(404, "document no longer staged")
        return Response(pdf, media_type="application/pdf", headers={
            "Content-Disposition": 'inline; filename="document.pdf"',
            "Content-Security-Policy": "sandbox",
        })

    @app.post("/v1/review/{txn_id}/decision")
    async def review_decision(
        txn_id: str, body: DecisionBody, who: Identity = Depends(principal("reviewer"))
    ) -> dict[str, Any]:
        corrections: Classification | None = None
        if body.corrections is not None:
            try:
                corrections = Classification.model_validate(body.corrections)
            except ValidationError as exc:
                raise HTTPException(422, "corrections do not match the classification schema") from exc
        try:
            res = await asyncio.to_thread(
                pipeline.review_resolve, txn(txn_id), who.name, body.decision,
                corrections=corrections, attest=body.attest, note=body.note,
            )
        except GateFailure as exc:
            raise HTTPException(409, {"code": exc.code, "message": exc.message}) from exc
        if body.decision == "approve" and res.ok:
            res = await orchestrator.advance(txn_id)
        return guarded(res)

    @app.post("/v1/approvals/{txn_id}")
    async def approve_filing(txn_id: str, who: Identity = Depends(principal("checker"))) -> dict[str, Any]:
        ctx = pipeline.store.load(txn(txn_id))
        if ctx is None or ctx.state not in {"AWAITING_APPROVAL", "SUBMITTED"}:
            raise HTTPException(409, "this transaction is not waiting for approval")
        return guarded(await orchestrator.advance(txn_id, approved_by=who.name))

    @app.post("/v1/payments/{txn_id}/reconcile")
    async def reconcile(txn_id: str, who: Identity = Depends(principal("checker"))) -> dict[str, Any]:
        try:
            res = await pipeline.reconcile_payment(txn(txn_id), who.name)
        except GateFailure as exc:
            raise HTTPException(409, {"code": exc.code, "message": exc.message}) from exc
        if res.ok and res.state == "PAID":
            res = await orchestrator.advance(txn_id)
        return guarded(res)

    @app.post("/v1/advance/{txn_id}")
    async def advance(txn_id: str, who: Identity = Depends(principal("operator"))) -> dict[str, Any]:
        """Run one transaction forward as far as it safely can (used by the n8n retry loop)."""
        return guarded(await orchestrator.advance(txn(txn_id)))

    # ---- step 1: e-signature ------------------------------------------------------------------
    if signer is not None:
        signing = SigningService(
            Path(settings.staging_dir) / "signing", signer,
            lambda pdf, source, name="": pipeline.ingest_bytes(pdf, source, name=name), pipeline.notifier,
        )

        async def _collect(envelope_id: str, background: BackgroundTasks) -> dict[str, Any]:
            try:
                rec, new_txn = await signing.collect(envelope_id)
            except GateFailure as exc:
                raise HTTPException(422, {"code": exc.code, "message": exc.message}) from exc
            if new_txn:
                background.add_task(orchestrator.advance, new_txn)  # signed documents flow on by themselves
            return {**rec.public(), "ingested": bool(new_txn)}

        @app.post("/v1/signing/requests")
        async def signing_request(
            body: SigningBody, who: Identity = Depends(principal("operator"))
        ) -> dict[str, Any]:
            try:
                pdf = base64.b64decode(body.pdf_base64, validate=True)
                rec = await signing.request(pdf, body.name, [Signer(s.name, s.email) for s in body.signers], who.name)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(422, "document is not valid base64") from exc
            except GateFailure as exc:
                raise HTTPException(422, {"code": exc.code, "message": exc.message}) from exc
            return rec.public()

        @app.get("/v1/signing/{envelope_id}")
        def signing_get(
            envelope_id: str, _who: Identity = Depends(principal("operator", "reviewer", "checker"))
        ) -> dict[str, Any]:
            try:
                return signing.get(envelope_id).public()
            except GateFailure as exc:
                raise HTTPException(404, exc.message) from exc

        @app.post("/v1/signing/{envelope_id}/collect")
        async def signing_collect(
            envelope_id: str, background: BackgroundTasks, _who: Identity = Depends(principal("operator"))
        ) -> dict[str, Any]:
            return await _collect(envelope_id, background)

        @app.post("/v1/signing/webhook")
        async def signing_webhook(
            request: Request, background: BackgroundTasks,
            x_signature: str | None = Header(default=None), x_timestamp: str | None = Header(default=None),
        ) -> dict[str, Any]:
            raw = await request.body()
            try:
                verify_webhook(raw, x_signature, x_timestamp, settings.webhook_secret)
                hook = EnvelopeHook.model_validate(json.loads(raw))
            except AuthError as exc:
                raise HTTPException(401, "unauthorised") from exc
            except (ValueError, ValidationError) as exc:
                raise HTTPException(422, "invalid body") from exc
            # The body only names an envelope. Everything else is re-read from the provider.
            return await _collect(hook.envelope_id, background)

    # ---- contract register and compliance monitoring --------------------------------------------
    reg = register or Register(Path(settings.staging_dir) / "register.sqlite3", pipeline.rules.penalty)
    reg_read = principal("operator", "reviewer", "checker")
    reg_write = principal("operator", "checker")

    def current_entries() -> list[RegEntry]:
        ctxs = [c for c in (pipeline.store.load(i) for i in pipeline.store.list_ids()) if c is not None]
        return reg.entries(ctxs, pipeline._today())

    @app.get("/v1/register")
    def register_list(
        status: str | None = None, kind: str | None = None, q: str | None = Query(default=None, max_length=100),
        _who: Identity = Depends(reg_read),
    ) -> dict[str, Any]:
        rows = current_entries()
        if status:
            rows = [e for e in rows if e.status.value == status.upper()]
        if kind:
            rows = [e for e in rows if e.doc_kind.value == kind.upper()]
        if q:
            needle = q.lower()
            rows = [e for e in rows if needle in f"{e.name} {e.entity} {e.counterparty or ''} {e.contract_owner or ''}".lower()]
        return {"items": [e.model_dump(mode="json") for e in rows[:1000]], "total": len(rows)}

    @app.post("/v1/register")
    def register_add(body: ManualIn, who: Identity = Depends(reg_write)) -> dict[str, str]:
        return {"id": reg.add_manual(body, who.name)}

    @app.post("/v1/register/import")
    async def register_import(request: Request, who: Identity = Depends(reg_write)) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > MAX_IMPORT_BYTES:
            raise HTTPException(413, "file too large")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "CSV must be UTF-8") from exc
        result = reg.import_csv(text, who.name)
        if result["errors"]:
            raise HTTPException(422, result)
        return result

    @app.patch("/v1/register/{entry_id}")
    def register_edit(entry_id: str, body: OverrideIn, who: Identity = Depends(reg_write)) -> dict[str, bool]:
        if not reg.update(entry_id, body, who.name):
            raise HTTPException(404, "entry not found or edit not valid")
        return {"ok": True}

    @app.get("/v1/register/{entry_id}/history")
    def register_history(entry_id: str, _who: Identity = Depends(reg_read)) -> dict[str, Any]:
        return {"items": reg.history(entry_id)}

    @app.get("/v1/register.csv")
    def register_csv(_who: Identity = Depends(reg_write)) -> Response:
        return Response(register_to_csv(current_entries()), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="stsds-register.csv"'})

    @app.get("/v1/monitoring/summary")
    def monitoring_summary(_who: Identity = Depends(reg_read)) -> dict[str, Any]:
        out: dict[str, Any] = json.loads(json.dumps(monitoring_summary_fn(current_entries(), pipeline._today()), default=str))
        return out

    @app.get("/v1/monitoring/alerts")
    def monitoring_alerts(_who: Identity = Depends(reg_read)) -> dict[str, Any]:
        return {"items": json.loads(json.dumps(monitoring_alerts_fn(current_entries(), pipeline._today()), default=str))}

    @app.get("/v1/monitoring/unstamped")
    def monitoring_unstamped(_who: Identity = Depends(reg_read)) -> dict[str, Any]:
        return {"items": [e.model_dump(mode="json") for e in unstamped_fn(current_entries())]}

    @app.get("/v1/reports/monthly")
    def report_monthly(
        month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), _who: Identity = Depends(reg_read)
    ) -> dict[str, Any]:
        rep = monthly_report_fn(current_entries(), int(month[:4]), int(month[5:]), pipeline._today())
        shaped: dict[str, Any] = json.loads(
            json.dumps(rep, default=lambda o: o.model_dump(mode="json") if hasattr(o, "model_dump") else str(o)))
        return shaped

    # ---- client master (step 0: register the client before any document is stamped) --------------
    if pipeline.clients is None:
        pipeline.clients = ClientDirectory(Path(settings.staging_dir) / "clients.sqlite3")
    cdir: ClientDirectory = pipeline.clients
    cl_read = principal("operator", "reviewer", "checker")
    cl_write = principal("operator", "reviewer", "checker")  # preparing a record; approval is separate
    cl_approve = principal("checker")  # always a named person, never the shared service token

    def client_json(c: Any) -> dict[str, Any]:
        out: dict[str, Any] = c.model_dump(mode="json")
        return out

    def cid_ok(cid: str) -> str:
        if len(cid) != 32 or any(ch not in "0123456789abcdef" for ch in cid):
            raise HTTPException(400, "invalid client id")
        return cid

    @app.get("/v1/clients")
    def clients_list(
        status: str | None = Query(default=None, pattern="^(DRAFT|ACTIVE|SUSPENDED|draft|active|suspended)$"),
        q: str | None = Query(default=None, max_length=100), _who: Identity = Depends(cl_read),
    ) -> dict[str, Any]:
        rows = cdir.find(status, q)
        return {"items": [client_json(c) for c in rows[:1000]], "total": len(rows)}

    @app.get("/v1/clients/pending")
    def clients_pending(_who: Identity = Depends(cl_read)) -> dict[str, Any]:
        ctxs = [c for c in (pipeline.store.load(i) for i in pipeline.store.list_ids()) if c is not None]
        return {"items": pending_from_holds(ctxs)}

    @app.post("/v1/clients")
    def clients_create(body: ClientIn, who: Identity = Depends(cl_write)) -> dict[str, str]:
        try:
            cid = cdir.create(body, who.name)
        except ValueError as exc:
            if str(exc) == "duplicate":
                raise HTTPException(409, "a client with this ID number is already registered") from exc
            raise HTTPException(422, "could not register the client") from exc
        return {"id": cid, "status": "DRAFT"}

    @app.get("/v1/clients/{cid}")
    def clients_get(cid: str, _who: Identity = Depends(cl_read)) -> dict[str, Any]:
        c = cdir.get(cid_ok(cid))
        if c is None:
            raise HTTPException(404, "client not found")
        return client_json(c)

    @app.patch("/v1/clients/{cid}")
    def clients_edit(cid: str, body: ClientPatch, who: Identity = Depends(cl_write)) -> dict[str, Any]:
        try:
            res = cdir.update(cid_ok(cid), body, who.name)
        except ValueError as exc:
            if str(exc) == "duplicate":
                raise HTTPException(409, "another client already has this ID number") from exc
            raise HTTPException(422, "invalid client data") from exc
        if res is None:
            raise HTTPException(404, "client not found")
        return res

    @app.post("/v1/clients/{cid}/activate")
    async def clients_activate(
        cid: str, background: BackgroundTasks, who: Identity = Depends(cl_approve)
    ) -> dict[str, Any]:
        try:
            cdir.activate(cid_ok(cid), who.name, pipeline._today())
        except KeyError as exc:
            raise HTTPException(404, "client not found") from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        released = pipeline.release_client_holds(who.name)  # documents held for this client move on by themselves

        async def _advance_all() -> None:
            for t in released:
                await orchestrator.advance(t)

        background.add_task(_advance_all)
        return {"status": "ACTIVE", "released": released}

    @app.post("/v1/clients/{cid}/suspend")
    def clients_suspend(cid: str, body: SuspendIn, who: Identity = Depends(cl_approve)) -> dict[str, bool]:
        if not cdir.suspend(cid_ok(cid), body.reason, who.name):
            raise HTTPException(404, "client not found or already suspended")
        return {"ok": True}

    @app.get("/v1/clients/{cid}/history")
    def clients_history(cid: str, _who: Identity = Depends(cl_read)) -> dict[str, Any]:
        return {"items": cdir.history(cid_ok(cid))}

    # ---- reviewer console (static, same-origin, no inline script) -------------------------------
    def console_file(name: str, media: str, csp: bool = False) -> Response:
        try:
            data = (CONSOLE_DIR / name).read_bytes()
        except OSError as exc:
            raise HTTPException(404, "not found") from exc
        headers = {"Content-Security-Policy": CONSOLE_CSP} if csp else {}
        return Response(data, media_type=media, headers=headers)

    @app.get("/console")
    def console_index() -> Response:
        return console_file("index.html", "text/html; charset=utf-8", csp=True)

    @app.get("/console/app.js")
    def console_js() -> Response:
        return console_file("app.js", "text/javascript; charset=utf-8")

    @app.get("/console/app.css")
    def console_css() -> Response:
        return console_file("app.css", "text/css; charset=utf-8")

    return app

"""Drives one transaction as far as it can safely go, then stops where a person is needed.

The orchestrator is the "agentic" layer: it decides the next stage from the persisted state, so it
is safe to call again at any time (after a crash, a restart, or a reviewer's decision). It never
decides anything the gates have not already allowed:

  INGESTED -> classify -> CLASSIFIED -> calculate -> CALCULATED -> preflight -> PREFLIGHT_OK
  -> draft (parity) -> PARITY_OK -> submit -> SUBMITTED -> pay -> PAID -> complete -> VAULTED
  -> verify -> COMPLETE

It stops (returning the reason) at: REVIEW (low score / gate needing a human), AWAITING_APPROVAL
(second person needed), the *_UNCERTAIN states (an irreversible step may or may not have happened),
HALTED (hard stop) and shadow mode (draft only). Nothing here retries submit or payment on its own.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .gates import GateFailure
from .pipeline import Pipeline, StageResult, TxnContext

log = logging.getLogger("stsds.orchestrator")
MAX_STEPS = 12
Step = tuple[str, Callable[[], Awaitable[StageResult]]]


class Orchestrator:
    def __init__(self, pipeline: Pipeline) -> None:
        self.p = pipeline
        self._locks: dict[str, asyncio.Lock] = {}
        self._work = asyncio.Semaphore(max(1, pipeline.s.batch_concurrency))
        self._portal = asyncio.Semaphore(max(1, pipeline.s.portal_concurrency))
        # Hook set by the batch service: may refuse a submit (e.g. batch payment cap reached).
        self.before_submit: Callable[[TxnContext], bool] | None = None

    def _lock(self, txn_id: str) -> asyncio.Lock:
        return self._locks.setdefault(txn_id, asyncio.Lock())

    async def _sync(self, fn: Callable[..., StageResult], *args: Any) -> StageResult:
        async with self._work:
            return await asyncio.to_thread(fn, *args)

    async def _portal_call(self, fn: Callable[..., Awaitable[StageResult]], *args: Any) -> StageResult:
        async with self._portal:  # one MyTax session at a time by default
            return await fn(*args)

    def _next(self, ctx: TxnContext, approved_by: str | None) -> Step | None:
        p, t, s = self.p, ctx.txn_id, ctx.state
        if s == "INGESTED":
            return "classify", lambda: self._sync(p.classify, t)
        if s == "CLASSIFIED":
            return "calculate", lambda: self._sync(p.calculate, t)
        if s == "CALCULATED":
            return "preflight", lambda: self._sync(p.preflight, t)
        if s == "PREFLIGHT_OK":
            return "draft", lambda: self._portal_call(p.draft, t)
        if s in {"PARITY_OK", "AWAITING_APPROVAL"}:
            if p.s.mode != "live":
                return None  # shadow mode: draft only
            if s == "AWAITING_APPROVAL" and not approved_by:
                return None
            return "submit", lambda: self._guarded_submit(ctx, approved_by)
        if s == "SUBMITTED":
            if p.payments is None:
                return "complete", lambda: self._portal_call(p.complete, t)
            return "pay", lambda: p.pay(t, approved_by)
        if s == "PAID":
            return "complete", lambda: self._portal_call(p.complete, t)
        if s == "VAULTED":
            return "verify", lambda: p.verify(t)
        return None  # COMPLETE, REVIEW, HALTED, REJECTED, *_UNCERTAIN, SUBMITTING, PAYING, VERIFY_FAILED

    async def _guarded_submit(self, ctx: TxnContext, approved_by: str | None) -> StageResult:
        # Defence in depth: only a passing score or a named human attestation may reach Submit.
        auto_ok = ctx.assessment is not None and ctx.assessment.decision == "AUTO"
        if not auto_ok and not ctx.human_verified:
            return self.p._halt(
                ctx, GateFailure("NOT_RELEASED", "Document was neither auto-cleared nor human-verified", "HALT"), "submit"
            )
        if self.before_submit is not None and not self.before_submit(ctx):
            return self.p._hold(
                ctx, GateFailure("BATCH_CAP", "Batch payment cap reached; held for an officer", "REVIEW"),
                ctx.state, "submit",
            )
        async with self._portal:
            return await self.p.submit(ctx.txn_id, approved_by)

    async def advance(self, txn_id: str, *, approved_by: str | None = None) -> StageResult:
        """Run the transaction forward until it completes or needs a person. Safe to call repeatedly."""
        async with self._lock(txn_id):
            last: StageResult | None = None
            for _ in range(MAX_STEPS):
                ctx = self.p.store.load(txn_id)
                if ctx is None:
                    return StageResult(
                        ok=False, txn_id=txn_id, state="UNKNOWN",
                        halt={"code": "TXN_UNKNOWN", "message": "Unknown transaction", "route": "HALT"},
                    )
                step = self._next(ctx, approved_by)
                if step is None:
                    return last or StageResult(
                        ok=True, txn_id=txn_id, state=ctx.state,
                        data={"waiting": True, "shadow": self.p.s.mode != "live" and ctx.state == "PARITY_OK"},
                    )
                name, call = step
                try:
                    res = await call()
                except GateFailure as exc:
                    res = self.p._halt(ctx, exc, name)
                except Exception as exc:  # noqa: BLE001 - any failure must park the txn, not crash the batch
                    log.error("unexpected %s in %s for %s", type(exc).__name__, name, txn_id)
                    try:
                        res = self.p.fail_internal(txn_id, name, exc)
                    except Exception as inner:  # noqa: BLE001 - even the failure path failed (audit store down?)
                        log.critical("could not record failure for %s: %s", txn_id, type(inner).__name__)
                        return StageResult(
                            ok=False, txn_id=txn_id, state="UNKNOWN",
                            halt={"code": "FAILURE_NOT_RECORDED", "message": "Stopped; the failure could not be recorded", "route": "HALT"},
                        )
                last = res
                if not res.ok:
                    return res
            return last or StageResult(ok=True, txn_id=txn_id, state="UNKNOWN")

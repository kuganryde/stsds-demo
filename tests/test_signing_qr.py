"""Step 1 (e-signature intake) and step 8 (QR verification of the stamp certificate)."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from stsds import demo
from stsds.api import create_app
from stsds.gates import GateFailure
from stsds.qr import QrVerification, check_qr_url, parse_hosts, read_qr_urls
from stsds.security import sign_webhook
from stsds.signing import Signer, SigningService, signature_gate, signature_report

NOW = dt.date(2026, 11, 10)
SIGNED_ON = dt.date(2026, 11, 8)
HOSTS = frozenset({"verify.demo.invalid"})
OPS = "o" * 40
SECRET = "w" * 40


def run(coro):
    return asyncio.run(coro)


def text(kind: str = "lease") -> str:
    return demo.sample_text(kind, SIGNED_ON) + "\n" + "Both parties agree to the terms set out in this document. " * 3


def signed(kind: str = "lease", n: int = 1) -> bytes:
    return demo.make_signed_pdf(text(kind), n)


def unsigned(kind: str = "lease") -> bytes:
    return demo.make_pdf(text(kind))


def kit_for(tmp_path, **kw):
    kw.setdefault("today", NOW)
    return demo.Kit(tmp_path, **kw)


async def one(kit, data):
    batch = await kit.batches.create("ops", "test", [("doc.pdf", data)])
    await kit.batches.run(batch.batch_id)
    return batch.entries[0].txn_id


# ---- QR: reading and URL safety ---------------------------------------------------------------
def test_qr_round_trip_and_absent_code():
    url = "https://verify.demo.invalid/certificate/DEMO-BILL-0001"
    assert read_qr_urls(demo.make_certificate("certificate text", url)) == [url]
    assert read_qr_urls(demo.make_certificate("certificate text", None)) == []


@pytest.mark.parametrize("url", [
    "http://verify.demo.invalid/c/1",                 # not https
    "https://user:pw@verify.demo.invalid/c/1",        # credentials in the URL
    "https://verify.demo.invalid:8443/c/1",           # unusual port
    "https://evil.example/c/1",                       # host not allow-listed
    "https://verify.demo.invalid.evil.example/c/1",   # look-alike host
    "https://verify.demo.invalid/c/1 extra",          # not a plain URL
    "javascript:alert(1)",                            # not a URL we open
    "https://verify.demo.invalid/" + "a" * 3000,      # absurd length
])
def test_unsafe_qr_urls_fail_closed(url):
    with pytest.raises(GateFailure):
        check_qr_url(url, HOSTS)


def test_good_qr_url_and_empty_allowlist():
    assert check_qr_url("https://VERIFY.demo.invalid/c/1", HOSTS) == "verify.demo.invalid"
    with pytest.raises(GateFailure) as exc:
        check_qr_url("https://verify.demo.invalid/c/1", frozenset())
    assert exc.value.code == "QR_UNCONFIGURED"
    assert parse_hosts(" A.example , b.example,,") == frozenset({"a.example", "b.example"})


def test_unreadable_certificate_never_loses_the_download(tmp_path):
    kit = kit_for(tmp_path)
    assert kit.pipeline._read_qr(b"junk")["found"] == "false"
    assert kit.pipeline._read_qr(b"%PDF-1.4 truncated")["found"] == "false"


# ---- QR: through the pipeline ------------------------------------------------------------------
def test_qr_pass_is_recorded_and_stored_with_the_vault_record(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        txn = await one(kit, unsigned())
        return kit.pipeline.store.load(txn)

    ctx = run(go())
    assert ctx.state == "COMPLETE" and ctx.verification["qr_verified"] == "PASS"
    assert ctx.qr and ctx.qr["found"] == "true" and ctx.qr["url"].startswith("https://verify.demo.invalid/")


@pytest.mark.parametrize("mode", ["no-qr", "foreign-host", "invalid"])
def test_bad_qr_stops_completion_even_though_everything_else_passed(tmp_path, mode):
    async def go():
        kit = kit_for(tmp_path)
        kit.portal.qr_mode = mode
        txn = await one(kit, unsigned())
        return kit, kit.pipeline.store.load(txn)

    kit, ctx = run(go())
    assert ctx.state == "VERIFY_FAILED"
    assert ctx.verification["qr_verified"].startswith("FAIL")
    assert ctx.verification["audit_chain"] == "PASS" and ctx.verification["payment_confirmed_by_provider"] == "PASS"
    assert ctx.vault is not None  # the certificate was still safely vaulted first


def test_foreign_host_is_never_opened_by_the_portal(tmp_path):
    opened: list[str] = []

    async def go():
        kit = kit_for(tmp_path)
        kit.portal.qr_mode = "foreign-host"
        original = kit.portal.verify_qr

        async def spy(url):
            opened.append(url)
            return await original(url)

        kit.portal.verify_qr = spy
        await one(kit, unsigned())

    run(go())
    assert opened == []


def test_unreadable_verification_page_is_skipped_not_passed(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        kit.portal.qr_mode = "hidden"
        return kit.pipeline.store.load(await one(kit, unsigned()))

    ctx = run(go())
    assert ctx.state == "COMPLETE" and ctx.verification["qr_verified"] == "SKIPPED"


def test_no_allowlisted_host_means_skipped(tmp_path):
    async def go():
        kit = kit_for(tmp_path, qr_verify_hosts="")
        return kit.pipeline.store.load(await one(kit, unsigned()))

    assert run(go()).verification["qr_verified"] == "SKIPPED"


def test_amount_on_verification_page_must_match_the_duty(tmp_path):
    async def go():
        kit = kit_for(tmp_path)

        async def wrong_amount(url):
            return QrVerification(True, "CERT", Decimal("0.01"))

        kit.portal.verify_qr = wrong_amount
        return kit.pipeline.store.load(await one(kit, unsigned()))

    ctx = run(go())
    assert ctx.state == "VERIFY_FAILED" and "differs" in ctx.verification["qr_verified"]


# ---- signatures: structure -----------------------------------------------------------------------
def test_signature_report_counts_and_detects_edits_after_signing():
    assert signature_report(signed(n=2)).total == 2
    assert signature_report(signed(n=2)).covering == 2
    assert signature_report(unsigned()).total == 0
    edited = signed() + b"\n% added after signing\n"
    assert signature_report(edited) == signature_report(edited).__class__(1, 0)


def test_signature_gate():
    assert signature_gate(unsigned(), 0).total == 0  # off by default
    with pytest.raises(GateFailure) as missing:
        signature_gate(unsigned(), 1)
    assert missing.value.code == "SIGNATURE_MISSING"
    with pytest.raises(GateFailure) as short:
        signature_gate(signed(n=1), 2)
    assert short.value.code == "SIGNATURE_MISSING"
    with pytest.raises(GateFailure) as broken:
        signature_gate(signed() + b"\n% tampered\n", 1)
    assert broken.value.code == "SIGNATURE_BROKEN"
    assert signature_gate(signed(n=2), 2).covering == 2


def test_unsigned_document_is_held_when_signatures_are_required(tmp_path):
    async def go():
        kit = kit_for(tmp_path, min_signatures=1)
        bad = await one(kit, unsigned())
        good = await one(kit, signed())
        return kit, bad, good

    kit, bad, good = run(go())
    held = kit.pipeline.store.load(bad)
    assert held.state == "REVIEW" and held.review.code == "SIGNATURE_MISSING" and held.review.resolvable is False
    done = kit.pipeline.store.load(good)
    assert done.state == "COMPLETE" and done.signatures == 1


def test_document_edited_after_signing_is_held(tmp_path):
    async def go():
        kit = kit_for(tmp_path, min_signatures=1)
        txn = await one(kit, signed() + b"\n% edited\n")
        return kit.pipeline.store.load(txn)

    ctx = run(go())
    assert ctx.state == "REVIEW" and ctx.review.code == "SIGNATURE_BROKEN"


# ---- signing service ----------------------------------------------------------------------------
def service(tmp_path, kit):
    signer = demo.SimulatedSigner()
    svc = SigningService(
        tmp_path / "signing", signer, lambda pdf, source, name="": kit.pipeline.ingest_bytes(pdf, source, name=name),
        kit.notifier,
    )
    return signer, svc


def test_sign_then_collect_ingests_exactly_once_and_flows_on(tmp_path):
    async def go():
        kit = kit_for(tmp_path, min_signatures=1)
        signer, svc = service(tmp_path, kit)
        rec = await svc.request(unsigned(), "lease.pdf", [Signer("Alpha", "a@example.com"), Signer("Beta", "b@example.com")], "ops")
        pending, nothing = await svc.collect(rec.envelope_id)
        signer.sign(rec.envelope_id)
        done, txn = await svc.collect(rec.envelope_id)
        again, second = await svc.collect(rec.envelope_id)
        res = await kit.orchestrator.advance(txn)
        return kit, rec, pending, nothing, done, txn, second, res

    kit, rec, pending, nothing, done, txn, second, res = run(go())
    assert pending.state == "PENDING" and nothing is None
    assert done.state == "COMPLETED" and txn and done.txn_id == txn
    assert second is None  # a repeat callback never ingests twice
    assert res.state == "COMPLETE"
    assert kit.pipeline.store.load(txn).signatures == 2
    saved = (tmp_path / "signing" / f"{rec.envelope_id}.json").read_text()
    assert "@example.com" not in saved  # signer emails stay with the provider


def test_declined_signing_alerts_and_ingests_nothing(tmp_path):
    async def go():
        kit = kit_for(tmp_path)
        signer, svc = service(tmp_path, kit)
        rec = await svc.request(unsigned(), "lease.pdf", [Signer("Alpha", "a@example.com")], "ops")
        signer.decline(rec.envelope_id)
        out, txn = await svc.collect(rec.envelope_id)
        return kit, out, txn

    kit, out, txn = run(go())
    assert out.state == "DECLINED" and txn is None
    assert kit.notifier.sent and kit.notifier.sent[-1][1] == "SIGNING_DECLINED"
    assert kit.pipeline.store.list_ids() == []


@pytest.mark.parametrize("signers", [
    [], [Signer("A", "not-an-email")], [Signer("A", "a@example.com\r\nbcc: x@evil.example")],
    [Signer("", "a@example.com")], [Signer("A", f"a{i}@example.com") for i in range(11)],
])
def test_bad_signers_are_refused(tmp_path, signers):
    async def go():
        kit = kit_for(tmp_path)
        _signer, svc = service(tmp_path, kit)
        await svc.request(unsigned(), "x.pdf", signers, "ops")

    with pytest.raises(GateFailure):
        run(go())


# ---- signing over HTTP -----------------------------------------------------------------------------
@pytest.fixture()
def world(tmp_path):
    kit = demo.Kit(tmp_path, today=NOW, engine_token=OPS, webhook_secret=SECRET, min_signatures=1)
    signer = demo.SimulatedSigner()
    return kit, signer, TestClient(create_app(kit.pipeline, kit.settings, None, signer))


def hook(client, envelope_id, secret=SECRET, ts=None):
    body = json.dumps({"envelope_id": envelope_id}).encode()
    ts = int(time.time()) if ts is None else ts
    return client.post("/v1/signing/webhook", content=body, headers={
        "X-Signature": sign_webhook(body, secret, ts), "X-Timestamp": str(ts)})


def test_signing_http_flow_end_to_end(world):
    kit, signer, c = world
    auth = {"Authorization": f"Bearer {OPS}"}
    assert c.post("/v1/signing/requests", json={}).status_code == 401
    body = {"name": "lease.pdf", "pdf_base64": base64.b64encode(unsigned()).decode(),
            "signers": [{"name": "Alpha", "email": "a@example.com"}]}
    r = c.post("/v1/signing/requests", headers=auth, json=body)
    assert r.status_code == 200 and r.json()["state"] == "PENDING"
    env = r.json()["envelope_id"]
    assert hook(c, env).json()["ingested"] is False  # not signed yet
    signer.sign(env)
    assert hook(c, env).json()["ingested"] is True
    status = c.get(f"/v1/signing/{env}", headers=auth).json()
    assert status["state"] == "COMPLETED"
    # the background advance ran the signed document all the way through
    assert kit.pipeline.store.load(status["txn_id"]).state == "COMPLETE"


def test_signing_webhook_rejects_forgeries_and_replays(world):
    _kit, _signer, c = world
    assert hook(c, "ENV-000001", secret="x" * 40).status_code == 401
    assert hook(c, "ENV-000001", ts=int(time.time()) - 3600).status_code == 401
    assert c.post("/v1/signing/webhook", content=b"{}").status_code == 401


def test_signing_rejects_bad_input(world):
    _kit, _signer, c = world
    auth = {"Authorization": f"Bearer {OPS}"}
    bad = c.post("/v1/signing/requests", headers=auth, json={
        "name": "x", "pdf_base64": "%%%", "signers": [{"name": "A", "email": "a@example.com"}]})
    assert bad.status_code == 422
    assert c.get("/v1/signing/..%2f..%2fetc", headers=auth).status_code in {404, 422}
    assert c.get("/v1/signing/ENV-999999", headers=auth).status_code == 404

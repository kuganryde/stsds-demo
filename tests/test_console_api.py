"""What the redesigned console depends on: who-am-I, batch list, transaction proof, signing list,
and the payment-retry queue visibility fix."""

from __future__ import annotations

import base64
import datetime as dt
import re
import time

import pytest
from fastapi.testclient import TestClient

from stsds import demo
from stsds.api import create_app
from stsds.users import UserDirectory, hash_token

NOW = dt.date(2026, 11, 10)
OPS, REV, CHK = "o" * 40, "r" * 40, "c" * 40


def hdr(t):
    return {"Authorization": f"Bearer {t}"}


def pdf64(kind="lease"):
    return base64.b64encode(demo.make_pdf(demo.sample_text(kind, NOW - dt.timedelta(days=2)))).decode()


@pytest.fixture
def world(tmp_path):
    kit = demo.Kit(tmp_path, today=NOW, engine_token=OPS)
    users = UserDirectory([
        {"name": "Rita", "roles": ["reviewer"], "token_sha256": hash_token(REV)},
        {"name": "Chen", "roles": ["checker"], "token_sha256": hash_token(CHK)},
    ])
    return kit, TestClient(create_app(kit.pipeline, kit.settings, users))


def run_batch(c, docs):
    made = c.post("/v1/batches", headers=hdr(OPS), json={"source": "t", "documents": [
        {"name": n, "pdf_base64": b} for n, b in docs], "auto_run": False}).json()
    c.post(f"/v1/batches/{made['batch_id']}/run", headers=hdr(OPS))
    return made["batch_id"]


def by_name(c, batch_id):
    return {d["name"]: d for d in c.get(f"/v1/batches/{batch_id}", headers=hdr(OPS)).json()["documents"]}


# ---- /v1/me -------------------------------------------------------------------------------------------
def test_me_reports_identity_roles_and_features(world):
    _k, c = world
    assert c.get("/v1/me").status_code == 401
    assert c.get("/v1/me", headers=hdr("z" * 40)).status_code == 401
    rev = c.get("/v1/me", headers=hdr(REV)).json()
    assert rev["name"] == "Rita" and rev["roles"] == ["reviewer"] and rev["threshold"] == 0.95
    assert rev["features"] == {"signing": False, "ledger": True} and rev["mode"] == "live"
    svc = c.get("/v1/me", headers=hdr(OPS)).json()
    assert svc["name"] == "service" and svc["roles"] == ["operator"]
    assert "token" not in str(svc).lower()


def test_me_reflects_the_signing_feature(tmp_path):
    kit = demo.Kit(tmp_path, today=NOW, engine_token=OPS)
    c = TestClient(create_app(kit.pipeline, kit.settings, None, demo.SimulatedSigner()))
    assert c.get("/v1/me", headers=hdr(OPS)).json()["features"]["signing"] is True


# ---- batches list and transaction proof -----------------------------------------------------------------
def test_batch_list_newest_first_and_readable_by_every_role(world):
    _k, c = world
    assert c.get("/v1/batches").status_code == 401
    first = run_batch(c, [("a.pdf", pdf64("lease"))])
    time.sleep(0.05)
    second = run_batch(c, [("b.pdf", pdf64("employment"))])
    for t in (REV, CHK, OPS):
        items = c.get("/v1/batches", headers=hdr(t)).json()["items"]
        assert [i["batch_id"] for i in items][:2] == [second, first]
    row = c.get("/v1/batches", headers=hdr(REV)).json()["items"][0]
    assert set(row) == {"batch_id", "status", "created_at", "created_by", "counts", "totals"}  # no document rows
    assert row["totals"]["documents"] == 1


def test_transaction_proof_shows_nine_checks_timeline_and_vault(world):
    _k, c = world
    bid = run_batch(c, [("lease.pdf", pdf64("lease"))])
    txn = by_name(c, bid)["lease.pdf"]["txn_id"]
    t = c.get(f"/v1/transactions/{txn}", headers=hdr(REV)).json()
    assert t["state"] == "COMPLETE" and t["duty_rm"] == t["lhdn_total_rm"]
    assert len(t["verification"]) == 9 and set(t["verification"].values()) == {"PASS"}
    assert "qr_verified" in t["verification"] and t["ledger_chain_valid"] is True
    stages = [r["stage"] for r in t["timeline"]]
    assert stages[0] == "INGESTED" and stages[-1] == "COMPLETE" and "PAID" in stages
    assert re.fullmatch(r"[0-9a-f]{64}", t["vault"]["bundle_sha256"]) and t["vault"]["retain_until"]
    assert "@" not in str(t)  # names and amounts only


def test_transaction_proof_rejects_bad_ids_and_unknown_documents(world):
    _k, c = world
    assert c.get("/v1/transactions/" + "0" * 32).status_code == 401
    assert c.get("/v1/transactions/not-an-id", headers=hdr(CHK)).status_code == 400
    assert c.get("/v1/transactions/" + "0" * 32, headers=hdr(CHK)).status_code == 404


# ---- the queue shows what a person must act on ------------------------------------------------------------
def test_unpaid_bill_after_reconcile_is_visible_to_a_checker_and_pays_once(world):
    kit, c = world
    kit.payments.fail_next = "timeout-before"  # no money moved, but the app cannot know that
    bid = run_batch(c, [("lease.pdf", pdf64("lease"))])
    txn = by_name(c, bid)["lease.pdf"]["txn_id"]
    q = c.get("/v1/review/queue", headers=hdr(CHK)).json()["items"]
    assert [(i["txn_id"], i["state"]) for i in q] == [(txn, "PAYMENT_UNCERTAIN")]
    out = c.post(f"/v1/payments/{txn}/reconcile", headers=hdr(CHK)).json()
    assert out["halt"]["code"] == "PAYMENT_RETRY_NEEDS_APPROVAL"  # parked, not silently lost
    q = c.get("/v1/review/queue", headers=hdr(CHK)).json()["items"]
    assert [(i["txn_id"], i["state"], i["code"]) for i in q] == [(txn, "SUBMITTED", "PAYMENT_RETRY_NEEDS_APPROVAL")]
    assert kit.payments.calls == 1  # reconciling never pays
    done = c.post(f"/v1/approvals/{txn}", headers=hdr(CHK)).json()
    assert done["state"] == "COMPLETE" and kit.payments.calls == 2 and len(kit.payments.paid) == 1
    assert c.get("/v1/review/queue", headers=hdr(CHK)).json()["items"] == []


def test_a_refused_payment_waits_in_the_queue_for_a_named_approver(world):
    kit, c = world
    kit.payments.fail_next = "reject"
    bid = run_batch(c, [("lease.pdf", pdf64("lease"))])
    txn = by_name(c, bid)["lease.pdf"]["txn_id"]
    q = c.get("/v1/review/queue", headers=hdr(CHK)).json()["items"]
    assert len(q) == 1 and q[0]["txn_id"] == txn and q[0]["state"] == "SUBMITTED" and q[0]["code"] == "PAYMENT_REJECTED"
    assert c.post(f"/v1/approvals/{txn}", headers=hdr(REV)).status_code == 403  # a reviewer cannot release money
    assert c.post(f"/v1/approvals/{txn}", headers=hdr(CHK)).json()["state"] == "COMPLETE"


def test_ordinary_in_flight_submitted_documents_are_not_listed(world):
    kit, c = world
    bid = run_batch(c, [("lease.pdf", pdf64("lease"))])
    assert by_name(c, bid)["lease.pdf"]["state"] == "COMPLETE"
    assert c.get("/v1/review/queue", headers=hdr(CHK)).json()["items"] == []


# ---- signing list -------------------------------------------------------------------------------------------
def test_signing_list_is_role_checked_and_names_only(tmp_path):
    kit = demo.Kit(tmp_path, today=NOW, engine_token=OPS, min_signatures=1)
    users = UserDirectory([{"name": "Rita", "roles": ["reviewer"], "token_sha256": hash_token(REV)}])
    c = TestClient(create_app(kit.pipeline, kit.settings, users, demo.SimulatedSigner()))
    assert c.get("/v1/signing").status_code == 401
    assert c.get("/v1/signing", headers=hdr(REV)).json() == {"items": []}
    from tests.test_signing_qr import unsigned
    body = {"name": "x.pdf", "pdf_base64": base64.b64encode(unsigned()).decode(),
            "signers": [{"name": "Alpha", "email": "alpha@example.com"}]}
    assert c.post("/v1/signing/requests", headers=hdr(OPS), json=body).status_code == 200
    items = c.get("/v1/signing", headers=hdr(REV)).json()["items"]
    assert len(items) == 1 and items[0]["name"] == "x.pdf" and items[0]["signers"] == ["Alpha"]
    assert "example.com" not in str(items)


# ---- the console itself ---------------------------------------------------------------------------------------
def test_console_shell_has_every_page_and_no_inline_style_or_script(world):
    _k, c = world
    html = c.get("/console").text
    for page in ("clients", "batches", "signing", "review", "approvals", "exceptions", "dashboard", "register", "ledger"):
        assert f'data-page="{page}"' in html and f'id="page-{page}"' in html
    assert 'id="page-detail"' in html and 'id="page-proof"' in html
    assert " style=" not in html and "<script>" not in html and "onclick=" not in html  # the CSP forbids all three
    js = c.get("/console/app.js").text
    assert "/v1/me" in js and "/v1/transactions/" in js and "innerHTML" not in js and "setAttribute(\"style\"" not in js
    assert "href=\"https://" not in html and "fonts.googleapis" not in c.get("/console/app.css").text


def test_console_fonts_are_self_hosted_and_allow_listed(world):
    client = world[1]
    """Fonts come from our own origin only: allow-listed names, same-origin CSP, no path traversal."""
    r = client.get("/console/fonts/ibm-plex-sans-latin-400-normal.woff2")
    assert r.status_code == 200 and r.headers["content-type"] == "font/woff2"
    assert "immutable" in r.headers["cache-control"]
    assert client.get("/console/fonts/passwd").status_code == 404
    assert client.get("/console/fonts/..%2Fapp.js").status_code == 404
    assert "font-src 'self'" in client.get("/console").headers["content-security-policy"]


def test_console_has_demo_and_labelled_form_hooks(world):
    client = world[1]
    html = client.get("/console").text
    for hook in ('id="demo-box"', 'id="demo-reviewer"', 'id="demo-checker"', 'id="demo-seed"', 'id="add-card"'):
        assert hook in html
    assert 'class="sr">Legal name' not in html and '<label for="c-name">Legal name</label>' in html
    js = client.get("/console/app.js").text
    assert "/demo/info" in js and "/demo/seed" in js and "innerHTML" not in js

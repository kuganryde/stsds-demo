from __future__ import annotations

import base64
import json
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from stsds.api import create_app
from stsds.rpa import PLACEHOLDER, SelectorMap
from stsds.security import (
    AuthError,
    mask,
    sign_webhook,
    valid_txn_id,
    verify_bearer,
    verify_webhook,
)

from .conftest import FakeRpa, make_cls, make_pdf

SECRET = "w" * 40
TOKEN = "t" * 40
LEASE_TEXT = "TENANCY AGREEMENT made on 2 November 2026. Monthly rent RM1,500.00 for a term of 12 months."


def signed(body: bytes, ts=None):
    ts = ts or int(time.time())
    return {"X-Signature": sign_webhook(body, SECRET, ts), "X-Timestamp": str(ts), "Content-Type": "application/json"}


@pytest.fixture()
def client(build):
    cls = make_cls("ITEM_49_LEASE", financials={"monthly_rental_rm": Decimal("1500.00"), "lease_duration_months": 12})
    pipe, *_ = build(cls, LEASE_TEXT, FakeRpa(Decimal("72.00")))
    return TestClient(create_app(pipe, pipe.s)), pipe


def test_webhook_signature_roundtrip_and_tamper():
    body = b'{"a":1}'
    ts = int(time.time())
    verify_webhook(body, sign_webhook(body, SECRET, ts), str(ts), SECRET)
    with pytest.raises(AuthError):
        verify_webhook(body + b" ", sign_webhook(body, SECRET, ts), str(ts), SECRET)
    with pytest.raises(AuthError):
        verify_webhook(body, sign_webhook(body, SECRET, ts), str(ts), "other" * 10)


def test_webhook_replay_window_and_missing_headers():
    body = b"{}"
    old = int(time.time()) - 3600
    with pytest.raises(AuthError):
        verify_webhook(body, sign_webhook(body, SECRET, old), str(old), SECRET)
    for sig, ts in [(None, "1"), ("x", None), ("x", "nope")]:
        with pytest.raises(AuthError):
            verify_webhook(body, sig, ts, SECRET)
    with pytest.raises(AuthError):
        verify_webhook(body, "x", str(int(time.time())), "")


def test_bearer_checks():
    verify_bearer(f"Bearer {TOKEN}", TOKEN)
    for header, token in [(None, TOKEN), ("Bearer nope", TOKEN), (f"Bearer {TOKEN}", ""), (f"Bearer {TOKEN}", "short")]:
        with pytest.raises(AuthError):
            verify_bearer(header, token)


def test_mask_and_txn_validation():
    assert mask("IG12345678090") == "*********8090" and mask(None) == ""
    assert valid_txn_id("a" * 32) and not valid_txn_id("../../etc/passwd") and not valid_txn_id("A" * 32)


def test_healthz_is_public_and_leaks_nothing_secret(client):
    c, _ = client
    r = c.get("/healthz")
    assert r.status_code == 200 and "token" not in r.text.lower()


def test_docs_endpoints_disabled(client):
    c, _ = client
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404


def test_ingest_requires_valid_signature(client):
    c, _ = client
    body = json.dumps({"source": "t", "document_id": "1", "pdf_base64": base64.b64encode(make_pdf(LEASE_TEXT)).decode()}).encode()
    assert c.post("/v1/ingest", content=body).status_code == 401
    assert c.post("/v1/ingest", content=body, headers=signed(body, int(time.time()) - 9999)).status_code == 401
    ok = c.post("/v1/ingest", content=body, headers=signed(body))
    assert ok.status_code == 200 and ok.json()["ok"] is True


def test_ingest_rejects_malformed_body_and_bad_pdf(client):
    c, _ = client
    junk = b"not json"
    assert c.post("/v1/ingest", content=junk, headers=signed(junk)).status_code == 422
    bad = json.dumps({"source": "t", "document_id": "1", "pdf_base64": base64.b64encode(b"hello").decode()}).encode()
    r = c.post("/v1/ingest", content=bad, headers=signed(bad))
    assert r.status_code == 200 and r.json()["halt"]["code"] == "PDF_MAGIC"


def test_stage_routes_need_bearer_and_valid_id(client):
    c, _ = client
    assert c.post("/v1/classify/" + "a" * 32).status_code == 401
    h = {"Authorization": f"Bearer {TOKEN}"}
    assert c.post("/v1/classify/not-a-txn", headers=h).status_code == 400
    assert c.get("/v1/audit/../etc/passwd", headers=h).status_code in (400, 404)


def test_full_http_flow_to_vault(client):
    c, pipe = client
    h = {"Authorization": f"Bearer {TOKEN}"}
    body = json.dumps({"source": "t", "document_id": "1", "pdf_base64": base64.b64encode(make_pdf(LEASE_TEXT)).decode()}).encode()
    txn = c.post("/v1/ingest", content=body, headers=signed(body)).json()["txn_id"]
    for path in ("classify", "calculate", "preflight"):
        assert c.post(f"/v1/{path}/{txn}", headers=h).json()["ok"]
    assert c.post(f"/v1/rpa/draft/{txn}", headers=h).json()["state"] == "PARITY_OK"
    assert c.post(f"/v1/rpa/submit/{txn}", headers=h, json={}).json()["state"] == "SUBMITTED"
    assert c.post(f"/v1/rpa/complete/{txn}", headers=h).json()["state"] == "VAULTED"
    audit = c.get(f"/v1/audit/{txn}", headers=h).json()
    assert audit["chain_valid"] and audit["records"][-1]["event"] == "VAULTED"


def test_calc_endpoint(client):
    c, _ = client
    h = {"Authorization": f"Bearer {TOKEN}"}
    payload = json.loads(make_cls("ITEM_22_SECURITY", financials={"consideration_or_loan_rm": Decimal("2000.01"), "security_kind": "SECURED_LOAN"}).model_dump_json())
    assert c.post("/v1/calc", headers=h, json=payload).json()["total_duty_rm"] == "15.00"
    assert c.post("/v1/calc", headers=h, json={"bogus": 1}).status_code == 422
    payload["financials"]["security_kind"] = "OTHER"
    assert c.post("/v1/calc", headers=h, json=payload).json()["needs_review"] is True


def test_oversize_body_rejected(client):
    c, pipe = client
    big = {"Content-Length": str(pipe.s.max_body_bytes + 1), "Authorization": f"Bearer {TOKEN}"}
    assert c.post("/v1/alert", headers=big, content=b"{}").status_code in (413, 400)


def test_selector_map_blocks_uncalibrated_worker(tmp_path):
    keys = dict.fromkeys(SelectorMap.GLOBAL_KEYS, "css")
    good = {**keys, "modules": {"SEWA_PAJAKAN": {"entry": "#a", "fields": {"monthly_rent": "#r"}}}}
    p = tmp_path / "s.json"
    p.write_text(json.dumps(good))
    assert SelectorMap.load(p).module("SEWA_PAJAKAN")["entry"] == "#a"
    from stsds.gates import GateFailure

    bad = {**good, "submit": PLACEHOLDER}
    p.write_text(json.dumps(bad))
    with pytest.raises(GateFailure) as e:
        SelectorMap.load(p)
    assert e.value.code == "RPA_UNCALIBRATED"
    p.write_text(json.dumps({"modules": {}}))
    with pytest.raises(GateFailure):
        SelectorMap.load(p)
    with pytest.raises(GateFailure):
        SelectorMap.load(tmp_path / "missing.json")


def test_settings_validation():
    from stsds.settings import Settings

    assert Settings.from_env({}).mode == "shadow"
    with pytest.raises(ValueError):
        Settings.from_env({"STSDS_MODE": "yolo"})

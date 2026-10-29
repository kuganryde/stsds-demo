"""Adapter tests: LLM client, notifier, TIN verifier, OCR text path, and Playwright mechanics
against a local mock portal (which proves the browser plumbing, not MyTax itself)."""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import threading
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from stsds import classifier as clf
from stsds.gates import GateFailure, HttpTinVerifier
from stsds.notify import TelegramNotifier
from stsds.ocr import PdfTextExtractor
from stsds.rpa import PlaywrightMyTax, SelectorMap

from .conftest import PARTIES, make_cls, make_pdf


class Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)  # type: ignore[arg-type]


def test_ollama_classifier_happy_path(monkeypatch):
    good = make_cls().model_dump_json()
    calls = []

    def fake_post(url, **kw):
        calls.append((url, kw["json"]))
        if url.endswith("/api/embeddings"):
            return Resp({"embedding": [0.1, 0.2]})
        if "/points/search" in url:
            return Resp({"result": [{"payload": {"text": "Item 49 lease"}}]})
        return Resp({"message": {"content": good}})

    monkeypatch.setattr(httpx, "post", fake_post)
    c = clf.OllamaClassifier("http://o", qdrant_url="http://q").classify("doc text")
    assert c.first_schedule_item.value == "ITEM_49_LEASE"
    chat = next(j for u, j in calls if u.endswith("/api/chat"))
    assert chat["options"] == {"temperature": 0.0, "seed": 0} and "<document>" in chat["messages"][1]["content"]
    assert "properties" in chat["format"]  # JSON schema constrains output


def test_ollama_failures_halt(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(GateFailure) as e:
        clf.OllamaClassifier("http://o").classify("x")
    assert e.value.code == "CLASSIFIER_UNAVAILABLE"
    with pytest.raises(GateFailure) as e2:
        clf.OllamaClassifier("http://o", qdrant_url="http://q").classify("x")
    assert e2.value.code == "RAG_UNAVAILABLE"


def test_telegram_notifier_sends_no_pii_and_swallows_errors(monkeypatch):
    sent = {}

    def ok(url, **kw):
        sent.update(kw["json"])
        return Resp({})

    monkeypatch.setattr(httpx, "post", ok)
    TelegramNotifier("tok", "chat").alert("a" * 32, "PARITY_MISMATCH", "engine differs", "HALT")
    assert "PARITY_MISMATCH" in sent["text"] and sent["chat_id"] == "chat"

    def fail(url, **kw):
        raise httpx.ConnectError("x")

    monkeypatch.setattr(httpx, "post", fail)
    TelegramNotifier("tok", "chat").alert("a" * 32, "X", "y", "HALT")  # must not raise


def test_http_tin_verifier(monkeypatch):
    party = PARTIES[0]
    monkeypatch.setattr(httpx, "post", lambda url, **kw: Resp({"valid": True}))
    assert HttpTinVerifier("http://v", "tok").verify(party) is True
    monkeypatch.setattr(httpx, "post", lambda url, **kw: Resp({"valid": "yes"}))
    assert HttpTinVerifier("http://v", "tok").verify(party) is False  # only literal true passes
    monkeypatch.setattr(httpx, "post", lambda url, **kw: Resp({}, 500))
    with pytest.raises(GateFailure) as e:
        HttpTinVerifier("http://v", "tok").verify(party)
    assert e.value.code == "TIN_VERIFIER_ERROR"


def test_ocr_uses_text_layer_and_halts_when_scan_needs_missing_engine():
    text = "TENANCY AGREEMENT\n" + "\n".join(["Monthly rent RM1,500.00 per month payable monthly."] * 6)
    assert "RM1,500.00" in PdfTextExtractor().extract(make_pdf(text))
    with pytest.raises(GateFailure) as e:
        PdfTextExtractor().extract(make_pdf(""))
    assert e.value.code == "OCR_UNAVAILABLE"


# ---- Playwright against the mock portal -----------------------------------------------------------
@pytest.fixture()
def portal(tmp_path):
    src = Path(__file__).parent / "portal"
    (tmp_path / "portal.html").write_text((src / "portal.html").read_text())
    (tmp_path / "sijil.pdf").write_bytes(make_pdf("SIJIL SETEM CERTIFICATE"))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (tmp_path / "state.json").write_text(json.dumps({"cookies": [], "origins": []}))
    sel = {
        "base_url": f"http://127.0.0.1:{server.server_port}", "new_bnds": "/portal.html",
        "save_draft": "#save", "summary_total": "#total", "draft_id": "#draftid", "draft_lookup": "#lookup",
        "submit": "#submit", "confirm_submit": "#confirm", "bill_no": "#bill", "virtual_account": "#va",
        "open_certificate": "#opencert", "download_sijil": "#dl",
        "party_add": "#party-add", "party_name": "#pname", "party_tin": "#ptin",
        "party_id_type": "#ptype", "party_id_number": "#pid",
        "modules": {"SEWA_PAJAKAN": {"entry": "#entry-lease", "fields": {
            "execution_date": "#date", "monthly_rent": "#rent", "lease_months": "#months", "duplicate_copies": "#dup"}}},
    }
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps(sel))
    yield PlaywrightMyTax(SelectorMap.load(path), str(tmp_path / "state.json")), tmp_path
    server.shutdown()


def test_playwright_draft_submit_download(portal):
    rpa, _ = portal
    fields = {"execution_date": "2026-11-02", "monthly_rent": "1500.00", "lease_months": "12", "duplicate_copies": "0"}
    parties = [{"name": "Alpha", "tin": "C1234567890", "id_type": "BRN", "id_number": "202001012345"}]
    draft = asyncio.run(rpa.draft("SEWA_PAJAKAN", fields, parties))
    assert draft.draft_id == "D-42" and draft.lhdn_total == Decimal("72.00")
    sub = asyncio.run(rpa.submit(draft.draft_id))
    assert sub.bill_no == "BILL-777" and sub.virtual_account == "VA-555"
    sijil = asyncio.run(rpa.download_sijil(draft.draft_id))
    assert sijil.startswith(b"%PDF-")


def test_playwright_unmapped_field_halts_before_touching_portal(portal):
    rpa, _ = portal
    with pytest.raises(GateFailure) as e:
        asyncio.run(rpa.draft("SEWA_PAJAKAN", {"surprise_field": "1"}, []))
    assert e.value.code == "RPA_FIELD_MAP"


def test_playwright_missing_element_halts_with_timeout(portal):
    rpa, tmp = portal
    rpa._timeout = 1500
    sel = json.loads((tmp / "selectors.json").read_text())
    sel["summary_total"] = "#does-not-exist"
    (tmp / "bad.json").write_text(json.dumps(sel))
    bad = PlaywrightMyTax(SelectorMap.load(tmp / "bad.json"), str(tmp / "state.json"), timeout_ms=1500)
    with pytest.raises(GateFailure) as e:
        asyncio.run(bad.draft("SEWA_PAJAKAN", {"monthly_rent": "1.00"}, []))
    assert e.value.code == "RPA_TIMEOUT"

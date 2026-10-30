"""Public hosted demo. NOT FOR PRODUCTION: simulated MyTax, payment and vault; nothing real is filed or paid.

Wraps the real API (``create_app``) and adds two demo-only routes so a visitor can try the whole loop
without the engine's secret operator token, which never leaves this process:

* ``GET  /demo/info``  the shared demo reviewer and checker tokens (they only unlock simulated data)
* ``POST /demo/seed``  submit a fresh mixed batch of sample contracts (rate-limited and capped)
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import os
import secrets
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from .api import create_app
from .clients import ClientDirectory, ClientIn
from .demo import Kit, make_pdf, sample_text
from .register import ManualIn, Register
from .users import UserDirectory, hash_token

SEED_COOLDOWN_S = 15.0
SEED_CAP = int(os.environ.get("DEMO_SEED_CAP", "60"))  # per process, so a public link cannot grow memory forever


def _seed_register(register: Register) -> None:
    """Fictional legacy contracts so the dashboard has something to show. Dates are relative to today."""
    today = dt.date.today()

    def d(days: int) -> dt.date:
        return today + dt.timedelta(days=days)

    rows = [
        ("Master services agreement (sample)", "Demo Alpha Sdn Bhd", "Demo Delta Sdn Bhd", "SERVICE", d(-120), "600.00", "Aisha Rahman", d(240), "CERT-DEMO-0001", d(-100)),
        ("Employment contract, J. Tan (sample)", "Demo Alpha Sdn Bhd", None, "EMPLOYMENT", d(-80), "10.00", "HR Team", None, "CERT-DEMO-0002", d(-75)),
        ("Office tenancy, Level 5 (sample)", "Demo Beta Sdn Bhd", "Demo Landlord Sdn Bhd", "TENANCY", d(-200), "1080.00", "Facilities", d(35), "CERT-DEMO-0003", d(-190)),
        ("Intercompany loan (sample)", "Demo Alpha Sdn Bhd", "Demo Gamma Sdn Bhd", "LOAN", d(-45), "1000.00", "Finance", None, None, None),
        ("Shareholders agreement (sample)", "Demo Gamma Sdn Bhd", None, "SHAREHOLDER", d(-26), "10.00", "Legal", None, None, None),
        ("Share transfer, Demo Gamma (sample)", "Demo Gamma Sdn Bhd", "Demo Beta Sdn Bhd", "SHARE_TRANSFER", d(-9), "900.00", "Legal", None, None, None),
        ("Warehouse tenancy (sample)", "Demo Beta Sdn Bhd", "Demo Landlord Sdn Bhd", "TENANCY", d(-400), "720.00", "Facilities", d(-15), "CERT-DEMO-0004", d(-380)),
    ]
    for name, entity, cp, kind, executed, duty, owner, expiry, cert, stamped in rows:
        register.add_manual(ManualIn(
            name=name, entity=entity, counterparty=cp, doc_kind=kind, execution_date=executed,
            duty_rm=Decimal(duty), contract_owner=owner, expiry_date=expiry, certificate_ref=cert,
            stamped_on=stamped), "demo seed")


def _seed_clients(directory: ClientDirectory, today: dt.date) -> None:
    """Two onboarded demo clients. Demo Omega is deliberately NOT onboarded: its sample document is held
    until a visitor registers it and a second person approves it. All names and IDs are fictional."""
    rows = [
        ("Demo Alpha Sdn Bhd", "202001012345", "C1234567890", "Aisha Rahman", "Engagement letter DEMO-EL-001",
         "Demo Alpha Finance", "Finance Controller"),
        ("Demo Beta Sdn Bhd", "201901054321", "C9876543210", "Daniel Lim", "Engagement letter DEMO-EL-002",
         "Demo Beta Admin", "Company Secretary"),
    ]
    for name, brn, tin, rm, auth, owner, role in rows:
        cid = directory.create(ClientIn(
            legal_name=name, entity_type="SDN_BHD", id_type="BRN", id_number=brn, tin=tin,
            registered_address="Level 1, Jalan Demo, 50000 Kuala Lumpur (fictional)",
            contact_name=owner, contact_role=role, relationship_manager=rm, default_contract_owner=owner,
            authority_ref=auth, authority_valid_until=today + dt.timedelta(days=365), approval_limit_rm=Decimal("5000"),
            signatories=[{"name": "Authorised Signatory", "designation": "Director"}],
        ), "Demo Onboarding")
        directory.activate(cid, "Demo Onboarding Approver", today)


def build() -> Any:
    work = Path(os.environ.get("DEMO_WORKDIR") or tempfile.mkdtemp(prefix="stsds-hosted-"))
    work.mkdir(parents=True, exist_ok=True)
    ops_token = secrets.token_hex(32)  # in-process only; never returned by any route
    reviewer = os.environ.get("DEMO_REVIEWER_TOKEN") or secrets.token_hex(20)
    checker = os.environ.get("DEMO_CHECKER_TOKEN") or secrets.token_hex(20)
    kit = Kit(work, engine_token=ops_token, webhook_secret=secrets.token_hex(32), require_registered_client=True)
    assert kit.pipeline.clients is not None
    _seed_clients(kit.pipeline.clients, dt.date.today())
    users = UserDirectory([
        {"name": "Demo Reviewer", "roles": ["reviewer"], "token_sha256": hash_token(reviewer)},
        {"name": "Demo Checker", "roles": ["checker"], "token_sha256": hash_token(checker)},
    ])
    register = Register(work / "register.sqlite3", kit.pipeline.rules.penalty)
    _seed_register(register)
    app = create_app(kit.pipeline, kit.settings, users, register=register)
    state = {"seeded": 0, "last": 0.0}
    lock = asyncio.Lock()

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/console")

    @app.get("/demo/info")
    def info() -> dict[str, Any]:
        return {"notice": "Simulated demo. Nothing is filed with MyTax or paid.",
                "reviewer_token": reviewer, "checker_token": checker,
                "seeded": state["seeded"], "seed_cap": SEED_CAP}

    @app.post("/demo/seed")
    async def seed() -> dict[str, Any]:
        async with lock:
            now = time.monotonic()
            if state["seeded"] >= SEED_CAP:
                raise HTTPException(429, "Demo batch limit reached. The demo resets when the service restarts.")
            if now - state["last"] < SEED_COOLDOWN_S:
                raise HTTPException(429, "Please wait a few seconds before loading another batch.")
            state["last"], n = now, state["seeded"]
            state["seeded"] += 1
        # A different signing date per batch changes the content hash, so repeats are not all duplicates.
        signed = dt.date.today() - dt.timedelta(days=1 + n % 25)
        lease = make_pdf(sample_text("lease", signed))
        docs = [
            ("lease-1500-x12.pdf", lease),
            ("employment-2800.pdf", make_pdf(sample_text("employment", signed))),
            ("loan-5m.pdf", make_pdf(sample_text("loan", signed))),
            ("lease-poor-scan.pdf", make_pdf(sample_text("blurry", signed))),
            ("lease-1500-x12-again.pdf", lease),
            ("lease-new-client-omega.pdf", make_pdf(sample_text("newclient", signed))),
            ("corrupt.pdf", b"this is not a pdf"),
        ]
        payload = {"source": "hosted-demo", "documents": [
            {"name": name, "pdf_base64": base64.b64encode(data).decode()} for name, data in docs]}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo") as c:
            r = await c.post("/v1/batches", json=payload, headers={"Authorization": f"Bearer {ops_token}"})
        if r.status_code != 200:
            raise HTTPException(502, "Could not create the demo batch.")
        out: dict[str, Any] = r.json()
        return out

    return app


app = build()

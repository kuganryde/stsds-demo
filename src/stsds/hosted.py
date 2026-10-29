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
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from .api import create_app
from .demo import Kit, make_pdf, sample_text
from .users import UserDirectory, hash_token

SEED_COOLDOWN_S = 15.0
SEED_CAP = int(os.environ.get("DEMO_SEED_CAP", "60"))  # per process, so a public link cannot grow memory forever


def build() -> Any:
    work = Path(os.environ.get("DEMO_WORKDIR") or tempfile.mkdtemp(prefix="stsds-hosted-"))
    work.mkdir(parents=True, exist_ok=True)
    ops_token = secrets.token_hex(32)  # in-process only; never returned by any route
    reviewer = os.environ.get("DEMO_REVIEWER_TOKEN") or secrets.token_hex(20)
    checker = os.environ.get("DEMO_CHECKER_TOKEN") or secrets.token_hex(20)
    kit = Kit(work, engine_token=ops_token, webhook_secret=secrets.token_hex(32))
    users = UserDirectory([
        {"name": "Demo Reviewer", "roles": ["reviewer"], "token_sha256": hash_token(reviewer)},
        {"name": "Demo Checker", "roles": ["checker"], "token_sha256": hash_token(checker)},
    ])
    app = create_app(kit.pipeline, kit.settings, users)
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

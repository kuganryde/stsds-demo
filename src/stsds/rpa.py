"""Node 6/8: MyTax e-Duti Setem RPA (Playwright).

Safety properties enforced here, independent of the pipeline:
  * Draft and submit are separate operations. ``draft`` never clicks Submit ("Hantar").
  * Selectors live in a calibration file. Any placeholder left in it halts the run, so an
    uncalibrated worker can never act on the live portal.
  * Authentication uses a pre-provisioned PKI session (``storage_state``) mounted read-only;
    credentials are never handled by this code.
  * The one-time Sijil Setem is read from the browser download stream and returned as bytes.

IMPORTANT: the selector map must be calibrated against the live portal during shadow-mode UAT
(blueprint Phase 5). This module cannot be validated against MyTax from outside LHDN's portal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .gates import GateFailure, parse_rm
from .qr import QrVerification

PLACEHOLDER = "UAT_REQUIRED"


@dataclass(frozen=True)
class DraftResult:
    draft_id: str
    lhdn_total: Decimal | None
    lines: dict[str, str]


@dataclass(frozen=True)
class SubmitResult:
    bill_no: str
    virtual_account: str | None


@dataclass(frozen=True)
class PortalBill:
    stamped: bool | None  # None = this deployment has no selector to read it


class MyTaxPort(Protocol):
    async def draft(
        self, module: str, fields: dict[str, str], parties: list[dict[str, str]]
    ) -> DraftResult: ...
    async def submit(self, draft_id: str) -> SubmitResult: ...
    async def download_sijil(self, draft_id: str) -> bytes: ...
    # Optional: ``async def bill_status(bill_no) -> PortalBill`` and
    # ``async def verify_qr(url) -> QrVerification``. The pipeline uses them when present.


class SelectorMap:
    GLOBAL_KEYS = (
        "base_url", "new_bnds", "save_draft", "summary_total", "submit", "confirm_submit",
        "bill_no", "virtual_account", "open_certificate", "download_sijil", "draft_id", "draft_lookup",
        "party_add", "party_name", "party_tin", "party_id_type", "party_id_number",
    )

    # Optional keys: if present they must be calibrated like the rest; if absent the check is skipped.
    OPTIONAL_KEYS = ("bill_lookup", "bill_stamped_marker", "qr_valid_marker", "qr_certificate_ref", "qr_amount")

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    @classmethod
    def load(cls, path: str | Path) -> SelectorMap:
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError) as exc:
            raise GateFailure("RPA_SELECTORS", f"Selector map unreadable: {exc}", "HALT") from exc
        sm = cls(data)
        sm.validate()
        return sm

    def validate(self) -> None:
        def walk(node: Any) -> list[str]:
            if isinstance(node, dict):
                return [s for v in node.values() for s in walk(v)]
            return [node] if isinstance(node, str) else []

        values = walk(self.data)
        missing = [k for k in self.GLOBAL_KEYS if k not in self.data]
        if missing or not self.data.get("modules"):
            raise GateFailure("RPA_SELECTORS", f"Selector map incomplete: {missing}", "HALT")
        if any(PLACEHOLDER in v for v in values):
            raise GateFailure(
                "RPA_UNCALIBRATED", "Selector map still holds UAT_REQUIRED placeholders", "HALT"
            )

    def module(self, name: str) -> dict[str, Any]:
        mod = self.data["modules"].get(name)
        if not mod:
            raise GateFailure("RPA_SELECTORS", f"No selectors for module {name}", "HALT")
        return mod  # type: ignore[no-any-return]


class PlaywrightMyTax:
    def __init__(
        self,
        selectors: SelectorMap,
        storage_state: str,
        headless: bool = True,
        timeout_ms: int = 30_000,
    ) -> None:
        self._sel = selectors
        self._state = storage_state
        self._headless = headless
        self._timeout = timeout_ms

    async def _page(self, pw: Any) -> tuple[Any, Any, Any]:
        browser = await pw.chromium.launch(headless=self._headless)
        context = await browser.new_context(storage_state=self._state, accept_downloads=True)
        context.set_default_timeout(self._timeout)
        return browser, context, await context.new_page()

    async def draft(
        self, module: str, fields: dict[str, str], parties: list[dict[str, str]]
    ) -> DraftResult:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        mod = self._sel.module(module)
        unknown = set(fields) - set(mod["fields"])
        if unknown:
            raise GateFailure("RPA_FIELD_MAP", f"No selector for fields: {sorted(unknown)}", "HALT")
        try:
            async with async_playwright() as pw:
                browser, _context, page = await self._page(pw)
                try:
                    await page.goto(self._sel.data["base_url"] + self._sel.data["new_bnds"])
                    await page.click(mod["entry"])
                    for name, value in fields.items():
                        await page.fill(mod["fields"][name], value)
                    for party in parties:
                        await page.click(self._sel.data["party_add"])
                        await page.fill(self._sel.data["party_name"], party["name"])
                        await page.fill(self._sel.data["party_tin"], party["tin"])
                        await page.select_option(self._sel.data["party_id_type"], party["id_type"])
                        await page.fill(self._sel.data["party_id_number"], party["id_number"])
                    await page.click(self._sel.data["save_draft"])  # saves a DRAFT only
                    total_text = await page.inner_text(self._sel.data["summary_total"])
                    draft_id = (await page.inner_text(self._sel.data["draft_id"])).strip()
                finally:
                    await browser.close()
        except PlaywrightError as exc:
            raise GateFailure("RPA_TIMEOUT", f"MyTax interaction failed: {exc}", "HALT") from exc
        return DraftResult(draft_id, parse_rm(total_text), {"summary_total": total_text.strip()})

    async def submit(self, draft_id: str) -> SubmitResult:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        try:
            async with async_playwright() as pw:
                browser, _context, page = await self._page(pw)
                try:
                    await page.goto(f"{self._sel.data['base_url']}{self._sel.data['new_bnds']}")
                    await page.fill(self._sel.data["draft_lookup"], draft_id)
                    await page.click(self._sel.data["submit"])  # binding Deemed Assessment
                    await page.click(self._sel.data["confirm_submit"])
                    bill = (await page.inner_text(self._sel.data["bill_no"])).strip()
                    va = (await page.inner_text(self._sel.data["virtual_account"])).strip()
                finally:
                    await browser.close()
        except PlaywrightError as exc:
            raise GateFailure("RPA_SUBMIT", f"Submit failed: {exc}", "HALT") from exc
        if not bill:
            raise GateFailure("RPA_SUBMIT", "No bill number returned after submit", "HALT")
        return SubmitResult(bill, va or None)

    async def bill_status(self, bill_no: str) -> PortalBill:
        """Read-only check that the portal shows the bill as stamped. Needs optional selectors."""
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        lookup, marker = self._sel.data.get("bill_lookup"), self._sel.data.get("bill_stamped_marker")
        if not lookup or not marker:
            return PortalBill(None)
        try:
            async with async_playwright() as pw:
                browser, _context, page = await self._page(pw)
                try:
                    await page.goto(f"{self._sel.data['base_url']}{self._sel.data['new_bnds']}")
                    await page.fill(lookup, bill_no)
                    return PortalBill(await page.locator(marker).count() > 0)
                finally:
                    await browser.close()
        except PlaywrightError as exc:
            raise GateFailure("RPA_STATUS", f"Could not read bill status: {exc}", "HALT") from exc

    async def verify_qr(self, url: str) -> QrVerification:
        """Open the (already allow-listed) QR URL and read what the verification page says."""
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        marker = self._sel.data.get("qr_valid_marker")
        if not marker:
            return QrVerification(None, detail="QR selectors not calibrated")
        start_host = (urlsplit(url).hostname or "").lower()
        try:
            async with async_playwright() as pw:
                browser, _context, page = await self._page(pw)
                try:
                    await page.goto(url)
                    if (urlsplit(page.url).hostname or "").lower() != start_host:
                        raise GateFailure("QR_REDIRECT", "Verification page redirected to another host", "HALT")
                    valid = await page.locator(marker).count() > 0
                    ref_sel, amt_sel = self._sel.data.get("qr_certificate_ref"), self._sel.data.get("qr_amount")
                    ref = (await page.inner_text(ref_sel)).strip() if ref_sel and await page.locator(ref_sel).count() else None
                    amount = parse_rm(await page.inner_text(amt_sel)) if amt_sel and await page.locator(amt_sel).count() else None
                    return QrVerification(valid, ref, amount)
                finally:
                    await browser.close()
        except PlaywrightError as exc:
            raise GateFailure("QR_PAGE", f"Could not read the QR verification page: {exc}", "HALT") from exc

    async def download_sijil(self, draft_id: str) -> bytes:
        """The portal allows exactly one click. Read the stream into memory immediately."""
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright

        try:
            async with async_playwright() as pw:
                browser, _context, page = await self._page(pw)
                try:
                    await page.goto(f"{self._sel.data['base_url']}{self._sel.data['new_bnds']}")
                    await page.fill(self._sel.data["draft_lookup"], draft_id)
                    await page.click(self._sel.data["open_certificate"])
                    async with page.expect_download() as info:
                        await page.click(self._sel.data["download_sijil"])
                    download = await info.value
                    path = await download.path()
                    if path is None:
                        raise GateFailure("SIJIL_STREAM", "Download stream had no data", "HALT")
                    return Path(path).read_bytes()
                finally:
                    await browser.close()
        except PlaywrightError as exc:
            raise GateFailure("SIJIL_STREAM", f"Sijil download failed: {exc}", "HALT") from exc

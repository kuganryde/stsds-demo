"""Payment of the stamp duty bill.

Money movement is the one step that cannot be undone, so this module is built around three rules:

  1. IDEMPOTENCY. Every payment carries a key derived from the transaction id. Asking twice can
     never pay twice, provided the provider honours the key (the HTTP adapter sends it as the
     ``Idempotency-Key`` header and the pipeline also checks the bill status before paying).
  2. DEFINITE vs UNCERTAIN failures. A 4xx from the provider means no money moved: halt, safe to
     investigate. A timeout, connection drop or 5xx means we do NOT KNOW: the pipeline parks the
     transaction as PAYMENT_UNCERTAIN, never retries on its own, and reconciles against the bill
     status before anything else happens.
  3. EXACT AMOUNT. The amount sent is the engine total that already matched LHDN's own figure to
     the sen, and the amount the provider reports back must equal it.

Which provider pays the bill (bank corporate API, FPX biller, treasury platform) is a deployment
decision. ``HttpPaymentProvider`` speaks a small documented JSON contract that a thin gateway in
front of the chosen provider can implement. MyTax itself only issues the bill and virtual account.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol

import httpx

from .gates import GateFailure

PayState = Literal["PAID", "UNPAID", "UNKNOWN"]


@dataclass(frozen=True)
class PaymentResult:
    reference: str
    amount_rm: Decimal
    status: PayState


@dataclass(frozen=True)
class BillStatus:
    payment: PayState
    amount_rm: Decimal | None = None
    reference: str | None = None
    stamped: bool | None = None  # None = the portal could not say


class PaymentPort(Protocol):
    async def pay(
        self, *, idempotency_key: str, bill_no: str, virtual_account: str | None, amount_rm: Decimal
    ) -> PaymentResult: ...

    async def status(self, bill_no: str) -> BillStatus: ...


def _money(value: object) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise GateFailure("PAYMENT_UNPARSEABLE", "Provider returned an unreadable amount", "HALT") from exc


class HttpPaymentProvider:
    """JSON contract (see README): POST /payments, GET /payments/{bill_no}. Bearer auth, TLS only."""

    def __init__(self, base_url: str | None, token: str | None, timeout: float = 30.0) -> None:
        self._url = (base_url or "").rstrip("/")
        self._token, self._timeout = token, timeout
        if self._url and not self._url.startswith("https://"):
            raise ValueError("payment provider URL must be https")

    def _check(self) -> None:
        if not self._url or not self._token:
            raise GateFailure("PAYMENT_UNCONFIGURED", "No payment provider is configured", "HALT")

    async def pay(
        self, *, idempotency_key: str, bill_no: str, virtual_account: str | None, amount_rm: Decimal
    ) -> PaymentResult:
        self._check()
        headers = {"Authorization": f"Bearer {self._token}", "Idempotency-Key": idempotency_key}
        payload = {
            "biller": "LHDN-STAMP-DUTY", "bill_no": bill_no,
            "virtual_account": virtual_account, "amount_rm": f"{amount_rm:.2f}",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(f"{self._url}/payments", json=payload, headers=headers)
        except httpx.HTTPError as exc:  # request may or may not have reached the provider
            raise GateFailure("PAYMENT_UNCERTAIN", f"No answer from provider: {type(exc).__name__}", "HALT") from exc
        if resp.status_code >= 500:
            raise GateFailure("PAYMENT_UNCERTAIN", f"Provider error {resp.status_code}", "HALT")
        if resp.status_code >= 400:  # definite refusal, no money moved
            raise GateFailure("PAYMENT_REJECTED", f"Provider refused the payment ({resp.status_code})", "HALT")
        try:
            data = resp.json()
            status: PayState = "PAID" if data.get("status") == "PAID" else "UNKNOWN"
            return PaymentResult(str(data["reference"])[:80], _money(data["amount_rm"]), status)
        except (ValueError, KeyError, TypeError) as exc:
            raise GateFailure("PAYMENT_UNCERTAIN", "Provider reply could not be understood", "HALT") from exc

    async def status(self, bill_no: str) -> BillStatus:
        self._check()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{self._url}/payments/{bill_no}", headers={"Authorization": f"Bearer {self._token}"}
                )
            if resp.status_code == 404:
                return BillStatus("UNPAID")
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GateFailure("PAYMENT_STATUS_ERROR", f"Could not read payment status: {type(exc).__name__}", "HALT") from exc
        state: PayState = {"PAID": "PAID", "UNPAID": "UNPAID"}.get(str(data.get("status")), "UNKNOWN")  # type: ignore[assignment]
        amount = _money(data["amount_rm"]) if data.get("amount_rm") is not None else None
        return BillStatus(state, amount, str(data.get("reference") or "")[:80] or None)

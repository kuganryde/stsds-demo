"""Strict data contracts. Money is Decimal (never float) and capped to two decimal places.

The classifier (LLM) output is validated against these models: any extra key, wrong type or
inconsistent module/item pairing is rejected, so a hallucinated field can never reach the engine.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

Money = Annotated[
    Decimal,
    Field(ge=0, le=Decimal("1000000000000"), decimal_places=2, allow_inf_nan=False),
]


class Item(str, Enum):
    ITEM_4_EMPLOYMENT = "ITEM_4_EMPLOYMENT"
    ITEM_49_LEASE = "ITEM_49_LEASE"
    ITEM_22_SECURITY = "ITEM_22_SECURITY"
    ITEM_32A_MOT = "ITEM_32A_MOT"
    ITEM_4_GENERAL = "ITEM_4_GENERAL"  # nominal-duty agreements (shareholder, intercompany, commercial)
    ITEM_22_AGREEMENT = "ITEM_22_AGREEMENT"  # loan and service agreements with an ascertainable sum
    ITEM_32_SHARES = "ITEM_32_SHARES"  # transfer of shares


class DocKind(str, Enum):
    """The document types TMF manages. Used by the contract register; the duty comes from the item."""

    EMPLOYMENT = "EMPLOYMENT"
    SERVICE = "SERVICE"
    INTERCOMPANY = "INTERCOMPANY"
    LOAN = "LOAN"
    SHAREHOLDER = "SHAREHOLDER"
    SHARE_TRANSFER = "SHARE_TRANSFER"
    TENANCY = "TENANCY"
    SECURITY = "SECURITY"
    COMMERCIAL = "COMMERCIAL"
    OTHER = "OTHER"


class Module(str, Enum):
    PENYETEMAN_AM = "PENYETEMAN_AM"
    SEWA_PAJAKAN = "SEWA_PAJAKAN"
    SEKURITI = "SEKURITI"
    PINDAH_MILIK_HARTANAH = "PINDAH_MILIK_HARTANAH"
    # Placeholder names for the three modules below: calibrate against the real BNDS screens.
    PERJANJIAN_AM = "PERJANJIAN_AM"
    PERJANJIAN_PINJAMAN = "PERJANJIAN_PINJAMAN"
    PEMINDAHAN_SAHAM = "PEMINDAHAN_SAHAM"


MODULE_FOR_ITEM: dict[Item, Module] = {
    Item.ITEM_4_EMPLOYMENT: Module.PENYETEMAN_AM,
    Item.ITEM_49_LEASE: Module.SEWA_PAJAKAN,
    Item.ITEM_22_SECURITY: Module.SEKURITI,
    Item.ITEM_32A_MOT: Module.PINDAH_MILIK_HARTANAH,
    Item.ITEM_4_GENERAL: Module.PERJANJIAN_AM,
    Item.ITEM_22_AGREEMENT: Module.PERJANJIAN_PINJAMAN,
    Item.ITEM_32_SHARES: Module.PEMINDAHAN_SAHAM,
}


class SecurityKind(str, Enum):
    SECURED_LOAN = "SECURED_LOAN"
    OTHER = "OTHER"


class IdType(str, Enum):
    NRIC = "NRIC"
    BRN = "BRN"
    PASSPORT = "PASSPORT"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Financials(Strict):
    monthly_salary_rm: Money | None = None
    monthly_rental_rm: Money | None = None
    lease_duration_months: int | None = Field(default=None, ge=1, le=1200)
    lease_indefinite: bool = False
    consideration_or_loan_rm: Money | None = None
    market_value_rm: Money | None = None
    security_kind: SecurityKind | None = None
    duplicate_copies: int = Field(default=0, ge=0, le=1000)
    fixed_tenure: bool | None = None  # loan or service agreement: does it state a fixed term?
    net_tangible_assets_rm: Money | None = None  # share transfers: NTA of the shares transferred


class Party(Strict):
    role: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    tin: str | None = Field(default=None, max_length=20)
    id_type: IdType
    id_number: str = Field(min_length=1, max_length=30)


class Classification(Strict):
    """Node 3 contract. Mirrors the blueprint's strict-JSON data contract."""

    bnds_module: Module
    first_schedule_item: Item
    requires_jpph_valuation: bool
    is_foreign_buyer_or_entity: bool
    is_residential_property: bool | None = None
    financials: Financials
    parties: list[Party] = Field(min_length=1, max_length=20)
    execution_date: dt.date
    document_kind: DocKind | None = None
    contract_owner: str | None = Field(default=None, max_length=200)
    expiry_date: dt.date | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def _module_matches_item(self) -> Classification:
        if MODULE_FOR_ITEM[self.first_schedule_item] != self.bnds_module:
            raise ValueError("bnds_module does not match first_schedule_item")
        return self


class Step(Strict):
    label: str
    formula: str
    amount_rm: Decimal


class CalcResult(Strict):
    item: Item
    exempt: bool
    primary_duty_rm: Decimal
    duplicate_duty_rm: Decimal
    total_duty_rm: Decimal
    steps: list[Step]
    rules_version: str
    rules_sha256: str
    engine_version: str


class ClockStatus(Strict):
    execution_date: dt.date
    deadline: dt.date
    days_remaining: int
    state: str  # OK | URGENT | OVERDUE

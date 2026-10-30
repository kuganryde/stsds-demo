"""Client master: who TMF is allowed to stamp for (step 0, before any document is submitted).

TMF files on behalf of client companies, so the app must know, before a document is read, that the
entity is a registered client, that TMF holds authority to act for it, and who at TMF looks after it.

Controls (all enforced here, not in the UI):
  * four-eyes: the person who prepared a record cannot be the one who activates it;
  * activation needs an authority reference (engagement letter or power of attorney) and valid IDs;
  * a material edit to an ACTIVE client (identity, TIN, authority, approval limit) sends it back to
    DRAFT, so documents are held until a second person re-approves;
  * a SUSPENDED client blocks its documents; reactivation needs someone other than the suspender;
  * every change is attributed and appended to a history table;
  * contact details are names and roles only. Emails and phone numbers stay in TMF's CRM.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import sqlite3
import threading
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .gates import _BRN_RE, _PASSPORT_RE, _TIN_RE, GateFailure, _valid_nric
from .schemas import IdType, Party

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_CLIENTS = 5000
MAX_SIGNATORIES = 10

# Gate codes. Only CLIENT_DETAILS_MISMATCH can be cleared by a reviewer (by correcting the extraction);
# the others are cleared by registering or approving the client, which releases the documents.
CLIENT_HOLD_CODES = frozenset({
    "CLIENT_NOT_REGISTERED", "CLIENT_NOT_ACTIVE", "CLIENT_SUSPENDED",
    "CLIENT_AUTHORITY_EXPIRED", "CLIENT_DETAILS_MISMATCH",
})
# Fields whose change invalidates an earlier approval.
MATERIAL_FIELDS = frozenset({
    "legal_name", "id_type", "id_number", "tin", "entity_type", "authority_ref",
    "authority_valid_until", "approval_limit_rm",
})


class ClientStatus(str, Enum):
    DRAFT = "DRAFT"  # prepared, waiting for a second person
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class EntityType(str, Enum):
    SDN_BHD = "SDN_BHD"
    BHD = "BHD"
    LLP = "LLP"
    PARTNERSHIP = "PARTNERSHIP"
    SOLE_PROPRIETOR = "SOLE_PROPRIETOR"
    INDIVIDUAL = "INDIVIDUAL"
    OTHER = "OTHER"


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Signatory(_S):
    name: str = Field(min_length=1, max_length=120)
    designation: str | None = Field(default=None, max_length=80)


class ClientIn(_S):
    legal_name: str = Field(min_length=1, max_length=200)
    entity_type: EntityType
    id_type: IdType = IdType.BRN
    id_number: str = Field(min_length=1, max_length=30)
    tin: str = Field(min_length=1, max_length=20)
    registered_address: str | None = Field(default=None, max_length=400)
    contact_name: str | None = Field(default=None, max_length=120)
    contact_role: str | None = Field(default=None, max_length=80)
    signatories: list[Signatory] = Field(default_factory=list, max_length=MAX_SIGNATORIES)
    relationship_manager: str = Field(min_length=1, max_length=120)  # the TMF person who owns this client
    default_contract_owner: str | None = Field(default=None, max_length=200)
    authority_ref: str | None = Field(default=None, max_length=120)  # engagement letter or PoA reference
    authority_valid_until: dt.date | None = None
    approval_limit_rm: Decimal | None = Field(default=None, gt=0, le=Decimal("1000000000000"), decimal_places=2)
    notes: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _ids(self) -> ClientIn:
        number, tin = self.id_number.strip().upper(), self.tin.strip().upper()
        ok = (
            (self.id_type is IdType.NRIC and _valid_nric(number))
            or (self.id_type is IdType.BRN and bool(_BRN_RE.match(number)))
            or (self.id_type is IdType.PASSPORT and bool(_PASSPORT_RE.match(number)))
        )
        if not ok:
            raise ValueError(f"invalid {self.id_type.value}")
        if not _TIN_RE.match(tin):
            raise ValueError("invalid TIN")
        return self


class ClientPatch(_S):
    """Partial edit. Only the fields that are sent change."""

    legal_name: str | None = Field(default=None, min_length=1, max_length=200)
    entity_type: EntityType | None = None
    id_type: IdType | None = None
    id_number: str | None = Field(default=None, min_length=1, max_length=30)
    tin: str | None = Field(default=None, min_length=1, max_length=20)
    registered_address: str | None = Field(default=None, max_length=400)
    contact_name: str | None = Field(default=None, max_length=120)
    contact_role: str | None = Field(default=None, max_length=80)
    signatories: list[Signatory] | None = Field(default=None, max_length=MAX_SIGNATORIES)
    relationship_manager: str | None = Field(default=None, min_length=1, max_length=120)
    default_contract_owner: str | None = Field(default=None, max_length=200)
    authority_ref: str | None = Field(default=None, max_length=120)
    authority_valid_until: dt.date | None = None
    approval_limit_rm: Decimal | None = Field(default=None, gt=0, le=Decimal("1000000000000"), decimal_places=2)
    notes: str | None = Field(default=None, max_length=1000)


class SuspendIn(_S):
    reason: str = Field(min_length=3, max_length=300)


class Client(BaseModel):
    """A stored client: the approved data plus its lifecycle fields."""

    id: str
    status: ClientStatus
    data: ClientIn
    prepared_by: str  # last person to create or materially change it
    created_by: str
    created_at: str
    activated_by: str | None = None
    activated_at: str | None = None
    suspended_by: str | None = None
    suspend_reason: str | None = None


class ClientMatch(BaseModel):
    """What the preflight gate learned about the parties of one document."""

    client_ids: list[str]
    primary_id: str
    primary_name: str
    default_contract_owner: str | None = None
    limit_exceeded_for: list[str] = []  # client names whose approval limit the duty reaches
    notes: list[str] = []


def normalise_id(value: str) -> str:
    """BRN/NRIC/passport compare case-insensitively and ignore spaces and hyphens."""
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


class ClientDirectory:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, idkey TEXT NOT NULL UNIQUE, "
                "status TEXT NOT NULL, body TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS client_history (n INTEGER PRIMARY KEY AUTOINCREMENT, "
                "at TEXT NOT NULL, actor TEXT NOT NULL, client_id TEXT NOT NULL, change TEXT NOT NULL)"
            )

    # ---- internals --------------------------------------------------------------------------
    def _log(self, actor: str, cid: str, change: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO client_history (at, actor, client_id, change) VALUES (?,?,?,?)",
            (_now(), actor[:80], cid, json.dumps(change, default=str)),
        )

    def _load(self, cid: str) -> Client | None:
        row = self._db.execute("SELECT body FROM clients WHERE id=?", (cid,)).fetchone()
        return Client.model_validate_json(row[0]) if row else None

    def _save(self, c: Client) -> None:
        self._db.execute(
            "UPDATE clients SET status=?, body=? WHERE id=?", (c.status.value, c.model_dump_json(), c.id)
        )

    @staticmethod
    def _who(actor: str) -> str:
        who = actor.strip()[:80]
        if not who:
            raise ValueError("a named person is required")
        return who

    # ---- writes -----------------------------------------------------------------------------
    def create(self, data: ClientIn, actor: str) -> str:
        """Create a DRAFT. Raises ValueError('duplicate') if that ID is already registered."""
        who = self._who(actor)
        cid = secrets.token_hex(16)
        client = Client(id=cid, status=ClientStatus.DRAFT, data=data, prepared_by=who, created_by=who,
                        created_at=_now())
        with self._lock, self._db:
            if self._db.execute("SELECT COUNT(*) FROM clients").fetchone()[0] >= MAX_CLIENTS:
                raise ValueError("client limit reached")
            try:
                self._db.execute(
                    "INSERT INTO clients (id, idkey, status, body) VALUES (?,?,?,?)",
                    (cid, normalise_id(data.id_number), client.status.value, client.model_dump_json()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("duplicate") from exc
            self._log(who, cid, {"created": data.legal_name})
        return cid

    def update(self, cid: str, patch: ClientPatch, actor: str) -> dict[str, Any] | None:
        """Apply a partial edit. Returns {'status', 'reapproval'} or None if unknown.

        Raises ValueError for an invalid result or a duplicate ID.
        """
        who = self._who(actor)
        if not _ID_RE.fullmatch(cid):
            return None
        changes = patch.model_dump(exclude_unset=True)
        with self._lock, self._db:
            client = self._load(cid)
            if client is None:
                return None
            merged = {**client.data.model_dump(), **changes}
            try:
                new = ClientIn.model_validate(merged)
            except ValidationError as exc:
                raise ValueError("invalid client data") from exc
            material = [k for k in changes if k in MATERIAL_FIELDS and
                        json.dumps(getattr(new, k), default=str) != json.dumps(getattr(client.data, k), default=str)]
            reapproval = bool(material) and client.status is ClientStatus.ACTIVE
            client.data = new
            if material:
                client.prepared_by = who
            if reapproval:
                client.status, client.activated_by, client.activated_at = ClientStatus.DRAFT, None, None
            try:
                self._db.execute("UPDATE clients SET idkey=? WHERE id=?", (normalise_id(new.id_number), cid))
            except sqlite3.IntegrityError as exc:
                raise ValueError("duplicate") from exc
            self._save(client)
            self._log(who, cid, {"edited": sorted(changes), "material": sorted(material), "reapproval": reapproval})
        return {"status": client.status.value, "reapproval": reapproval}

    def activate(self, cid: str, actor: str, today: dt.date) -> Client:
        """Second-person approval. Also reactivates a suspended client (by someone other than the suspender)."""
        who = self._who(actor)
        with self._lock, self._db:
            client = self._load(cid) if _ID_RE.fullmatch(cid) else None
            if client is None:
                raise KeyError(cid)
            if client.status is ClientStatus.ACTIVE:
                return client
            if who.casefold() == client.prepared_by.casefold():
                raise PermissionError("four-eyes: a different person must approve what you prepared")
            if client.status is ClientStatus.SUSPENDED and who.casefold() == (client.suspended_by or "").casefold():
                raise PermissionError("four-eyes: a different person must reactivate a suspended client")
            d = client.data
            if not (d.authority_ref or "").strip():
                raise ValueError("an authority reference (engagement letter or power of attorney) is required")
            if d.authority_valid_until is not None and d.authority_valid_until < today:
                raise ValueError("the authority on file has expired")
            client.status, client.activated_by, client.activated_at = ClientStatus.ACTIVE, who, _now()
            client.suspended_by = client.suspend_reason = None
            self._save(client)
            self._log(who, cid, {"activated": True})
        return client

    def suspend(self, cid: str, reason: str, actor: str) -> bool:
        who = self._who(actor)
        with self._lock, self._db:
            client = self._load(cid) if _ID_RE.fullmatch(cid) else None
            if client is None or client.status is ClientStatus.SUSPENDED:
                return False
            client.status, client.suspended_by, client.suspend_reason = ClientStatus.SUSPENDED, who, reason[:300]
            self._save(client)
            self._log(who, cid, {"suspended": reason[:300]})
        return True

    # ---- reads ------------------------------------------------------------------------------
    def get(self, cid: str) -> Client | None:
        if not _ID_RE.fullmatch(cid):
            return None
        with self._lock:
            return self._load(cid)

    def find(self, status: str | None = None, q: str | None = None) -> list[Client]:
        with self._lock:
            rows = self._db.execute("SELECT body FROM clients ORDER BY rowid").fetchall()
        out = [Client.model_validate_json(r[0]) for r in rows]
        if status:
            out = [c for c in out if c.status.value == status.upper()]
        if q:
            needle = q.lower()
            out = [c for c in out if needle in
                   f"{c.data.legal_name} {c.data.id_number} {c.data.relationship_manager}".lower()]
        return out

    def history(self, cid: str) -> list[dict[str, Any]]:
        if not _ID_RE.fullmatch(cid):
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT at, actor, change FROM client_history WHERE client_id=? ORDER BY n", (cid,)).fetchall()
        return [{"at": a, "actor": who, "change": json.loads(c)} for a, who, c in rows]

    def by_id_number(self, id_number: str) -> Client | None:
        with self._lock:
            row = self._db.execute(
                "SELECT body FROM clients WHERE idkey=?", (normalise_id(id_number),)).fetchone()
        return Client.model_validate_json(row[0]) if row else None


# ---- the preflight gate ---------------------------------------------------------------------
def client_gate(
    directory: ClientDirectory | None, parties: list[Party], today: dt.date, duty_rm: Decimal | None = None,
) -> ClientMatch:
    """Raise a GateFailure unless at least one party is a registered, active client with valid authority.

    Counterparties need not be clients. Every party that IS a client must be clear: one suspended or
    unapproved client blocks the document even if another party is fine.
    """
    if directory is None:
        raise GateFailure("CLIENT_REGISTRY_UNAVAILABLE", "The client register is not available", "HALT")
    matched: list[tuple[Party, Client]] = []
    for p in parties:
        hit = directory.by_id_number(p.id_number)
        if hit is not None:
            matched.append((p, hit))
    if not matched:
        names = ", ".join(p.name for p in parties) or "unknown parties"
        raise GateFailure(
            "CLIENT_NOT_REGISTERED",
            f"None of the parties is a registered client ({names}). Register the client; this document "
            "is released automatically when the client is approved.",
        )
    notes: list[str] = []
    for party, c in matched:
        label = c.data.legal_name
        if c.status is ClientStatus.SUSPENDED:
            raise GateFailure("CLIENT_SUSPENDED", f"{label} is suspended: {c.suspend_reason or 'no reason recorded'}")
        if c.status is not ClientStatus.ACTIVE:
            raise GateFailure("CLIENT_NOT_ACTIVE", f"{label} is registered but not yet approved by a second person")
        until = c.data.authority_valid_until
        if until is not None and until < today:
            raise GateFailure("CLIENT_AUTHORITY_EXPIRED", f"TMF's authority for {label} expired on {until}")
        if (party.tin or "").upper() != c.data.tin.strip().upper():
            raise GateFailure(
                "CLIENT_DETAILS_MISMATCH",
                f"The TIN on the document for {label} does not match the client record. Check the document "
                "and correct the extraction if it was misread.",
            )
        if normalise_id(party.name) != normalise_id(c.data.legal_name):
            notes.append(f"Name on the document differs from the client record: {party.name}")
    first = matched[0][1]
    exceeded = [c.data.legal_name for _p, c in matched
                if duty_rm is not None and c.data.approval_limit_rm is not None and duty_rm >= c.data.approval_limit_rm]
    return ClientMatch(
        client_ids=[c.id for _p, c in matched], primary_id=first.id, primary_name=first.data.legal_name,
        default_contract_owner=first.data.default_contract_owner, limit_exceeded_for=exceeded, notes=notes,
    )


def pending_from_holds(contexts: list[Any]) -> list[dict[str, Any]]:
    """Group the parties of documents held as CLIENT_NOT_REGISTERED, to prefill the onboarding form."""
    groups: dict[str, dict[str, Any]] = {}
    for ctx in contexts:
        if ctx.state != "REVIEW" or ctx.review is None or ctx.review.code != "CLIENT_NOT_REGISTERED":
            continue
        c = ctx.classification or ctx.candidate
        if c is None:
            continue
        for p in c.parties:
            key = normalise_id(p.id_number)
            g = groups.setdefault(key, {"name": p.name, "id_type": p.id_type.value, "id_number": p.id_number,
                                        "tin": p.tin, "documents": 0})
            g["documents"] += 1
    return sorted(groups.values(), key=lambda g: (-g["documents"], g["name"]))

"""Contract register and compliance monitoring (TMF services 3 and 4).

The register has two sources that are merged on read:
  * pipeline entries: derived from the authoritative transaction records (never typed in), with a
    small set of human-maintained overrides (owner, entity, renewal and expiry dates, notes);
  * manual entries: contracts that were stamped or still need stamping outside this pipeline
    (legacy contracts, CSV import).

Everything here is read-only with respect to filing: it never submits, pays or changes a
transaction. Penalty figures are ESTIMATES for exposure reporting (see rules notes).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import secrets
import sqlite3
import threading
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .gates import URGENT_DAYS, sec47_clock
from .ledger import csv_safe
from .pipeline import TxnContext
from .rules import Penalty
from .schemas import DocKind, Item

MAX_IMPORT_ROWS = 2000
MAX_IMPORT_BYTES = 1_000_000
EXPIRY_WARN_DAYS = 90
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_KIND_FOR_ITEM = {
    Item.ITEM_4_EMPLOYMENT: DocKind.EMPLOYMENT,
    Item.ITEM_49_LEASE: DocKind.TENANCY,
    Item.ITEM_22_SECURITY: DocKind.SECURITY,
    Item.ITEM_32A_MOT: DocKind.OTHER,
    Item.ITEM_4_GENERAL: DocKind.COMMERCIAL,
    Item.ITEM_22_AGREEMENT: DocKind.LOAN,
    Item.ITEM_32_SHARES: DocKind.SHARE_TRANSFER,
}


class Status(str, Enum):
    STAMPED = "STAMPED"
    IN_PROGRESS = "IN_PROGRESS"  # in the pipeline, or waiting for a person
    UNSTAMPED = "UNSTAMPED"  # not stamped and still inside the 30 days
    OVERDUE = "OVERDUE"  # not stamped and past the 30 days
    NOT_REQUIRED = "NOT_REQUIRED"  # a person decided no stamping is needed


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManualIn(_S):
    name: str = Field(min_length=1, max_length=200)
    entity: str = Field(min_length=1, max_length=200)
    counterparty: str | None = Field(default=None, max_length=200)
    doc_kind: DocKind
    execution_date: dt.date
    duty_rm: Decimal | None = Field(default=None, ge=0, le=Decimal("1000000000000"), decimal_places=2)
    contract_owner: str | None = Field(default=None, max_length=200)
    certificate_ref: str | None = Field(default=None, max_length=120)
    stamped_on: dt.date | None = None
    renewal_date: dt.date | None = None
    expiry_date: dt.date | None = None
    not_required: bool = False
    notes: str | None = Field(default=None, max_length=1000)


class OverrideIn(_S):
    contract_owner: str | None = Field(default=None, max_length=200)
    entity: str | None = Field(default=None, max_length=200)
    renewal_date: dt.date | None = None
    expiry_date: dt.date | None = None
    not_required: bool | None = None
    notes: str | None = Field(default=None, max_length=1000)


class Penalty_(_S):
    days_late: int
    percent: int
    amount_rm: Decimal
    duty_known: bool


class Entry(_S):
    id: str
    source: str  # pipeline | manual
    txn_id: str | None = None
    name: str
    entity: str
    counterparty: str | None = None
    doc_kind: DocKind
    contract_owner: str | None = None
    execution_date: dt.date
    deadline: dt.date
    days_to_deadline: int
    duty_rm: Decimal | None = None
    status: Status
    certificate_ref: str | None = None
    stamped_on: dt.date | None = None
    renewal_date: dt.date | None = None
    expiry_date: dt.date | None = None
    penalty: Penalty_ | None = None
    attention: list[str] = []
    notes: str | None = None


def _add_months(d: dt.date, months: int) -> dt.date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    last = (dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(year, month, min(d.day, last))


def penalty_estimate(
    duty: Decimal | None, deadline: dt.date, as_of: dt.date, rules_penalty: Penalty
) -> Penalty_ | None:
    """Estimated late-stamping penalty if the instrument is stamped on ``as_of``. None if not late."""
    if as_of <= deadline:
        return None
    for band in rules_penalty.bands:
        if band.within_months is None or as_of <= _add_months(deadline, band.within_months):
            pct_amount = (duty * band.pct / 100) if duty is not None else Decimal(0)
            amount = max(band.min_rm, pct_amount).quantize(Decimal("0.01"), ROUND_HALF_UP)
            return Penalty_(days_late=(as_of - deadline).days, percent=band.pct, amount_rm=amount,
                            duty_known=duty is not None)
    return None  # unreachable: the last band is open (validated)


class Register:
    def __init__(self, path: str | Path, penalty: Penalty) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._penalty = penalty
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        with self._lock, self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS manual (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS overrides (key TEXT PRIMARY KEY, body TEXT NOT NULL)")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS history (n INTEGER PRIMARY KEY AUTOINCREMENT, "
                "at TEXT NOT NULL, actor TEXT NOT NULL, key TEXT NOT NULL, change TEXT NOT NULL)"
            )

    # ---- writes (every change is attributed and recorded) -------------------------------------
    def _log(self, actor: str, key: str, change: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO history (at, actor, key, change) VALUES (?,?,?,?)",
            (dt.datetime.now(dt.UTC).isoformat(), actor, key, json.dumps(change, default=str, sort_keys=True)),
        )

    def add_manual(self, data: ManualIn, actor: str) -> str:
        cid = secrets.token_hex(16)
        with self._lock, self._db:
            self._db.execute("INSERT INTO manual (id, body) VALUES (?,?)", (cid, data.model_dump_json()))
            self._log(actor, cid, {"created": data.model_dump(mode="json")})
        return cid

    def update(self, entry_id: str, patch: OverrideIn, actor: str) -> bool:
        """Edit a manual entry in place, or store overrides for a pipeline entry (key = txn id)."""
        if not _ID_RE.fullmatch(entry_id):
            return False
        changes = patch.model_dump(exclude_unset=True, mode="json")
        with self._lock, self._db:
            row = self._db.execute("SELECT body FROM manual WHERE id=?", (entry_id,)).fetchone()
            if row is not None:
                merged = {**json.loads(row[0]), **changes}
                try:
                    ManualIn.model_validate(merged)
                except ValidationError:
                    return False
                self._db.execute("UPDATE manual SET body=? WHERE id=?", (json.dumps(merged), entry_id))
            else:
                old = self._db.execute("SELECT body FROM overrides WHERE key=?", (entry_id,)).fetchone()
                merged = {**(json.loads(old[0]) if old else {}), **changes}
                self._db.execute(
                    "INSERT INTO overrides (key, body) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET body=excluded.body", (entry_id, json.dumps(merged)))
            self._log(actor, entry_id, {"updated": changes})
        return True

    def history(self, entry_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT at, actor, change FROM history WHERE key=? ORDER BY n", (entry_id,)).fetchall()
        return [{"at": a, "actor": who, "change": json.loads(c)} for a, who, c in rows]

    def import_csv(self, text: str, actor: str) -> dict[str, Any]:
        """Bulk-load legacy contracts. Header row required. Rejects the whole file on a bad row."""
        if len(text.encode()) > MAX_IMPORT_BYTES:
            return {"imported": 0, "errors": ["file too large"]}
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows or len(rows) > MAX_IMPORT_ROWS:
            return {"imported": 0, "errors": [f"between 1 and {MAX_IMPORT_ROWS} rows required"]}
        parsed: list[ManualIn] = []
        errors: list[str] = []
        for n, row in enumerate(rows, start=2):
            clean = {k.strip(): (v or "").strip() or None for k, v in row.items() if k}
            if "not_required" in clean:
                clean["not_required"] = (clean["not_required"] or "").lower() in ("1", "true", "yes", "y")
            try:
                if clean.get("duty_rm") is not None:
                    Decimal(str(clean["duty_rm"]))
                parsed.append(ManualIn.model_validate(clean))
            except (ValidationError, InvalidOperation) as exc:
                errors.append(f"row {n}: {str(exc).splitlines()[0][:120]}")
        if errors:
            return {"imported": 0, "errors": errors[:20]}
        for item in parsed:
            self.add_manual(item, actor)
        return {"imported": len(parsed), "errors": []}

    # ---- reads --------------------------------------------------------------------------------
    def _overrides(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT key, body FROM overrides").fetchall()
        return {k: json.loads(b) for k, b in rows}

    def _manual(self) -> list[tuple[str, ManualIn]]:
        with self._lock:
            rows = self._db.execute("SELECT id, body FROM manual").fetchall()
        return [(i, ManualIn.model_validate_json(b)) for i, b in rows]

    def _build(self, *, eid: str, source: str, txn_id: str | None, name: str, entity: str,
               counterparty: str | None, kind: DocKind, owner: str | None, executed: dt.date,
               deadline: dt.date, duty: Decimal | None, stamped: bool, stamped_on: dt.date | None,
               cert: str | None, renewal: dt.date | None, expiry: dt.date | None,
               not_required: bool, in_progress: bool, notes: str | None, attention: list[str],
               today: dt.date) -> Entry:
        days = (deadline - today).days
        if not_required:
            status = Status.NOT_REQUIRED
        elif stamped:
            status = Status.STAMPED
        elif days < 0:
            status = Status.OVERDUE
        elif in_progress:
            status = Status.IN_PROGRESS
        else:
            status = Status.UNSTAMPED
        pen = None
        if status in (Status.OVERDUE, Status.IN_PROGRESS, Status.UNSTAMPED):
            pen = penalty_estimate(duty, deadline, today, self._penalty)
        elif status is Status.STAMPED and stamped_on is not None:
            pen = penalty_estimate(duty, deadline, stamped_on, self._penalty)  # incurred, for the record
        return Entry(
            id=eid, source=source, txn_id=txn_id, name=name, entity=entity, counterparty=counterparty,
            doc_kind=kind, contract_owner=owner, execution_date=executed, deadline=deadline,
            days_to_deadline=days, duty_rm=duty, status=status, certificate_ref=cert, stamped_on=stamped_on,
            renewal_date=renewal, expiry_date=expiry, penalty=pen, attention=attention, notes=notes)

    def entries(self, contexts: list[TxnContext], today: dt.date) -> list[Entry]:
        out: list[Entry] = []
        ov = self._overrides()
        for ctx in contexts:
            c = ctx.classification or ctx.candidate
            if c is None:
                continue
            o = ov.get(ctx.txn_id, {})
            deadline = ctx.clock.deadline if ctx.clock else sec47_clock(
                c.execution_date, today, ctx.received_in_malaysia).deadline
            stamped = ctx.state in ("VAULTED", "COMPLETE", "VERIFY_FAILED")
            attention = ["Verification failed: a person must check this certificate"] if ctx.state == "VERIFY_FAILED" else []
            if ctx.state in ("REJECTED", "HALTED"):
                attention.append("Stopped: " + (ctx.last_halt or {}).get("code", ctx.state))
            elif ctx.state in ("REVIEW", "AWAITING_APPROVAL", "PAYMENT_UNCERTAIN", "SUBMIT_UNCERTAIN"):
                attention.append("Waiting for a person: " + ctx.state.replace("_", " ").lower())
            f = c.financials
            expiry = o.get("expiry_date") or c.expiry_date
            if expiry is None and f.lease_duration_months and not f.lease_indefinite:
                expiry = _add_months(c.execution_date, f.lease_duration_months)
            names = [p.name for p in c.parties]
            out.append(self._build(
                eid=ctx.txn_id, source="pipeline", txn_id=ctx.txn_id, name=ctx.name or ctx.txn_id[:8],
                entity=o.get("entity") or ctx.client_name or (names[0] if names else "Unknown"),
                counterparty=names[1] if len(names) > 1 else None,
                kind=c.document_kind or _KIND_FOR_ITEM[c.first_schedule_item],
                owner=o.get("contract_owner") or c.contract_owner or ctx.client_owner, executed=c.execution_date, deadline=deadline,
                duty=ctx.calc.total_duty_rm if ctx.calc else None, stamped=stamped, stamped_on=ctx.stamped_on,
                cert=(ctx.vault or {}).get("sijil_sha256", "")[:16] or None,
                renewal=dt.date.fromisoformat(o["renewal_date"]) if o.get("renewal_date") else None,
                expiry=dt.date.fromisoformat(expiry) if isinstance(expiry, str) else expiry,
                not_required=bool(o.get("not_required")), in_progress=ctx.state not in ("REJECTED", "HALTED"),
                notes=o.get("notes"), attention=attention, today=today))
        for mid, m in self._manual():
            deadline = m.execution_date + dt.timedelta(days=30)
            out.append(self._build(
                eid=mid, source="manual", txn_id=None, name=m.name, entity=m.entity,
                counterparty=m.counterparty, kind=m.doc_kind, owner=m.contract_owner,
                executed=m.execution_date, deadline=deadline, duty=m.duty_rm,
                stamped=m.stamped_on is not None or bool(m.certificate_ref), stamped_on=m.stamped_on,
                cert=m.certificate_ref, renewal=m.renewal_date, expiry=m.expiry_date,
                not_required=m.not_required, in_progress=False, notes=m.notes, attention=[], today=today))
        out.sort(key=lambda e: (e.deadline, e.name))
        return out


# ---- monitoring (pure functions over entries) ---------------------------------------------------
def alerts(entries: list[Entry], today: dt.date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in entries:
        if e.status is Status.OVERDUE:
            p = e.penalty
            tail = f" Estimated penalty RM{p.amount_rm}" + ("" if p and p.duty_known else " (minimum; duty unknown)") if p else ""
            out.append({"level": "OVERDUE", "id": e.id, "name": e.name, "entity": e.entity, "date": e.deadline,
                        "days": e.days_to_deadline, "message": f"Stamping deadline passed {-e.days_to_deadline} days ago.{tail}"})
        elif e.status in (Status.UNSTAMPED, Status.IN_PROGRESS) and e.days_to_deadline <= URGENT_DAYS:
            out.append({"level": "DUE_SOON", "id": e.id, "name": e.name, "entity": e.entity, "date": e.deadline,
                        "days": e.days_to_deadline, "message": f"Stamping deadline in {e.days_to_deadline} days."})
        if e.status is not Status.NOT_REQUIRED:
            for label, when in (("Renewal", e.renewal_date), ("Expiry", e.expiry_date)):
                if when is None:
                    continue
                left = (when - today).days
                if left < 0:
                    out.append({"level": "EXPIRED", "id": e.id, "name": e.name, "entity": e.entity, "date": when,
                                "days": left, "message": f"{label} date passed {-left} days ago."})
                elif left <= EXPIRY_WARN_DAYS:
                    out.append({"level": "EXPIRING", "id": e.id, "name": e.name, "entity": e.entity, "date": when,
                                "days": left, "message": f"{label} in {left} days."})
    order = {"OVERDUE": 0, "EXPIRED": 1, "DUE_SOON": 2, "EXPIRING": 3}
    out.sort(key=lambda a: (order[a["level"]], a["days"], a["name"]))
    return out


def unstamped(entries: list[Entry]) -> list[Entry]:
    return [e for e in entries if e.status in (Status.UNSTAMPED, Status.OVERDUE, Status.IN_PROGRESS)]


def summary(entries: list[Entry], today: dt.date) -> dict[str, Any]:
    active = [e for e in entries if e.status is not Status.NOT_REQUIRED]
    stamped = [e for e in active if e.status is Status.STAMPED]
    exposure = sum((e.penalty.amount_rm for e in active if e.penalty and e.status is not Status.STAMPED), Decimal(0))
    incurred = sum((e.penalty.amount_rm for e in stamped if e.penalty), Decimal(0))
    al = alerts(entries, today)
    by_entity: Counter[str] = Counter()
    for e in unstamped(entries):
        by_entity[e.entity] += 1
    return {
        "as_of": today, "total": len(entries),
        "by_status": dict(Counter(e.status.value for e in entries)),
        "by_kind": dict(Counter(e.doc_kind.value for e in entries)),
        "unstamped_by_entity": dict(by_entity.most_common(10)),
        "duty_stamped_rm": sum((e.duty_rm or Decimal(0) for e in stamped), Decimal(0)),
        "compliance_rate_pct": round(100 * len(stamped) / len(active), 1) if active else None,
        "penalty_exposure_rm": exposure, "penalty_incurred_rm": incurred,
        "alerts": dict(Counter(a["level"] for a in al)),
        "penalty_note": "Estimates only: bands to be confirmed by the Head of Tax. Nothing is filed or paid.",
    }


def monthly_report(entries: list[Entry], year: int, month: int, today: dt.date) -> dict[str, Any]:
    def in_month(d: dt.date | None) -> bool:
        return d is not None and d.year == year and d.month == month
    return {
        "month": f"{year:04d}-{month:02d}", "as_of": today,
        "executed": [e for e in entries if in_month(e.execution_date)],
        "stamped": [e for e in entries if in_month(e.stamped_on)],
        "outstanding": unstamped(entries),
        "late_stamped": [e for e in entries if e.status is Status.STAMPED and e.penalty is not None],
        "expiring": [a for a in alerts(entries, today) if a["level"] in ("EXPIRING", "EXPIRED")],
        "summary": summary(entries, today),
    }


COLUMNS = ["name", "entity", "counterparty", "doc_kind", "contract_owner", "execution_date", "deadline",
           "duty_rm", "status", "stamped_on", "certificate_ref", "renewal_date", "expiry_date",
           "penalty_estimate_rm", "source"]


def to_csv(entries: list[Entry]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for e in entries:
        d = e.model_dump(mode="json")
        d["doc_kind"] = e.doc_kind.value
        d["status"] = e.status.value
        d["penalty_estimate_rm"] = str(e.penalty.amount_rm) if e.penalty else ""
        w.writerow([csv_safe(d.get(c)) for c in COLUMNS])
    return buf.getvalue()

"""Per-document confidence score.

The score answers one question: "how safe is it to let this document run without a human?"
It is the PRODUCT of independent components, so one weak signal pulls the whole score down and a
zero in any component (an ungrounded figure, a missing input, an overdue clock) makes it zero.
That is deliberately conservative: the cost of sending a good document to a reviewer is minutes,
the cost of auto-filing a bad one is a binding Deemed Assessment.

The score never replaces a gate. Gates stay hard; the score only decides AUTO vs REVIEW for
documents that pass them, and gives the reviewer a ranked, explained queue.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from . import engine
from .gates import GateFailure, grounding_report, preflight_parties
from .rules import Rules
from .schemas import Classification, ClockStatus, Party

MIN_TEXT_CHARS = 40  # only near-empty extractions are penalised; real contracts run to thousands


class _FormatOnly:
    """Verifier stub: lets us reuse the format checks of preflight_parties without any lookup."""

    def verify(self, party: Party) -> bool:
        return True


class Assessment(BaseModel):
    score: float
    threshold: float
    decision: Literal["AUTO", "REVIEW"]
    components: dict[str, float]
    reasons: list[str] = []


def _party_score(parties: list[Party]) -> tuple[float, list[str]]:
    if not parties:
        return 0.0, ["no parties extracted"]
    good, reasons = 0, []
    for p in parties:
        try:
            preflight_parties([p], _FormatOnly())
            good += 1
        except GateFailure as exc:
            reasons.append(exc.message)
    return good / len(parties), reasons


def assess(
    c: Classification,
    text: str,
    clock: ClockStatus,
    rules: Rules,
    rules_sha256: str,
    threshold: float,
) -> Assessment:
    reasons: list[str] = []
    missing, checked = grounding_report(c, text)
    grounding = (checked - len(missing)) / checked
    if missing:
        reasons.append("not found in document: " + ", ".join(sorted(missing)))

    parties, party_reasons = _party_score(c.parties)
    reasons.extend(party_reasons)

    completeness = 1.0
    if c.requires_jpph_valuation:
        completeness = 0.0
        reasons.append("JPPH valuation required")
    else:
        try:
            engine.calculate(c, rules, rules_sha256)
        except engine.NeedsReview as exc:
            completeness = 0.0
            reasons.append(exc.message)

    clock_score = 0.0 if clock.state == "OVERDUE" else 1.0
    if clock.state == "OVERDUE":
        reasons.append("30-day stamping window has passed")

    text_quality = min(1.0, len(text.strip()) / MIN_TEXT_CHARS)
    if text_quality < 1.0:
        reasons.append("very little text could be read from the document")

    if c.confidence < threshold:
        reasons.append(f"model confidence {c.confidence:.2f} is below {threshold:.2f}")

    components = {
        "model_confidence": round(c.confidence, 4),
        "grounding": round(grounding, 4),
        "parties": round(parties, 4),
        "completeness": completeness,
        "clock": clock_score,
        "text_quality": round(text_quality, 4),
    }
    score = 1.0
    for value in components.values():
        score *= value
    score = round(score, 4)
    decision: Literal["AUTO", "REVIEW"] = "AUTO" if score >= threshold else "REVIEW"
    return Assessment(
        score=score, threshold=threshold, decision=decision, components=components, reasons=reasons
    )

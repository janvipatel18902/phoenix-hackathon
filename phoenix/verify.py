"""External verification reads against mock_world (contracts/CONTRACTS.md section 3/5).

Every write is confirmed by a read here before a task is ever counted as
progress (invariant 3). A read that fails or comes back in a shape we don't
recognize is "cannot determine" -- never a guessed success or failure
(invariant 4): callers must route that into NEEDS_HUMAN, never VERIFIED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from phoenix.models import TaskKind


@dataclass
class ExternalReadResult:
    applied: bool | None  # None = cannot determine
    matches_expected: bool | None  # only meaningful when applied is True
    observation: dict[str, Any] = field(default_factory=dict)


def _matches(observation: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(observation.get(k) == v for k, v in expected.items())


def verify_cancel_ticket(
    client: httpx.Client, attendee_id: str, expected: dict[str, Any]
) -> ExternalReadResult:
    try:
        resp = client.get(f"/ticketing/attendees/{attendee_id}")
    except httpx.RequestError:
        return ExternalReadResult(applied=None, matches_expected=None)
    if resp.status_code != 200:
        return ExternalReadResult(applied=None, matches_expected=None)
    try:
        attendee = resp.json()
        observation = {
            "ticket_id": attendee["ticket_id"],
            "ticket_status": attendee["ticket_status"],
        }
    except (KeyError, ValueError):
        return ExternalReadResult(applied=None, matches_expected=None)
    applied = observation["ticket_status"] == "cancelled"
    return ExternalReadResult(
        applied=applied,
        matches_expected=_matches(observation, expected) if applied else None,
        observation=observation,
    )


def _refund_amount_minor(refund: dict[str, Any]) -> int:
    """Payments v1 returns dollars as a decimal string; v2 returns amount_minor."""
    if "amount_minor" in refund:
        return int(refund["amount_minor"])
    if "amount" in refund:
        return int((Decimal(refund["amount"]) * 100).to_integral_value())
    raise KeyError("refund has neither amount_minor nor amount")


def verify_refund(
    client: httpx.Client, idempotency_key: str, expected: dict[str, Any]
) -> ExternalReadResult:
    try:
        resp = client.get("/payments/refunds", params={"idempotency_key": idempotency_key})
    except httpx.RequestError:
        return ExternalReadResult(applied=None, matches_expected=None)
    if resp.status_code != 200:
        return ExternalReadResult(applied=None, matches_expected=None)
    try:
        refunds = resp.json()["refunds"]
    except (KeyError, ValueError):
        return ExternalReadResult(applied=None, matches_expected=None)
    if not refunds:
        return ExternalReadResult(applied=False, matches_expected=None)
    try:
        refund = refunds[0]
        observation = {
            "attendee_id": refund["attendee_id"],
            "amount_minor": _refund_amount_minor(refund),
            "currency": refund["currency"],
        }
    except (KeyError, ValueError):
        return ExternalReadResult(applied=None, matches_expected=None)
    return ExternalReadResult(
        applied=True, matches_expected=_matches(observation, expected), observation=observation
    )


def verify_notify(
    client: httpx.Client, reference: str, expected: dict[str, Any]
) -> ExternalReadResult:
    try:
        resp = client.get("/notifications/emails", params={"reference": reference})
    except httpx.RequestError:
        return ExternalReadResult(applied=None, matches_expected=None)
    if resp.status_code != 200:
        return ExternalReadResult(applied=None, matches_expected=None)
    try:
        emails = resp.json()["emails"]
    except (KeyError, ValueError):
        return ExternalReadResult(applied=None, matches_expected=None)
    sent_count = len(emails)
    applied = sent_count >= 1
    observation = {
        "reference": reference,
        "template": emails[0]["template"] if emails else None,
        "sent_count": sent_count,
    }
    return ExternalReadResult(
        applied=applied,
        matches_expected=_matches(observation, expected) if applied else None,
        observation=observation,
    )


def verify_release_venue(
    client: httpx.Client, booking_id: str, expected: dict[str, Any]
) -> ExternalReadResult:
    try:
        resp = client.get(f"/venue/bookings/{booking_id}")
    except httpx.RequestError:
        return ExternalReadResult(applied=None, matches_expected=None)
    if resp.status_code != 200:
        return ExternalReadResult(applied=None, matches_expected=None)
    try:
        booking = resp.json()
        observation = {"booking_id": booking["booking_id"], "status": booking["status"]}
    except (KeyError, ValueError):
        return ExternalReadResult(applied=None, matches_expected=None)
    applied = observation["status"] == "cancelled"
    return ExternalReadResult(
        applied=applied,
        matches_expected=_matches(observation, expected) if applied else None,
        observation=observation,
    )


_VERIFIERS = {
    TaskKind.CANCEL_TICKET: verify_cancel_ticket,
    TaskKind.REFUND: verify_refund,
    TaskKind.NOTIFY: verify_notify,
    TaskKind.RELEASE_VENUE: verify_release_venue,
}


def verify_task(client: httpx.Client, task: dict[str, Any]) -> ExternalReadResult:
    """Dispatch to the right verifier by task kind.

    REFUND verifies by idempotency_key (survives state loss); the others key
    off the task's `ref` (attendee_id or booking_id).
    """
    kind = TaskKind(task["kind"])
    verifier = _VERIFIERS[kind]
    if kind == TaskKind.REFUND:
        return verifier(client, task["idempotency_key"], task["expected_effect"])
    return verifier(client, task["ref"], task["expected_effect"])

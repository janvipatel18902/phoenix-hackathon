"""The task state machine: transitions, enforced via CAS + the allowed-transitions
table in models.py. State transition + attempt/effect record are written in one
Mongo transaction wherever a second document is involved (invariant 6).
"""

from __future__ import annotations

from typing import Any

from pymongo import ReturnDocument
from pymongo.database import Database

import phoenix.db as db_module
from phoenix.db import ATTEMPTS, EFFECTS, TASKS, now_iso
from phoenix.intents import request_hash
from phoenix.models import (
    ALLOWED_TRANSITIONS,
    AttemptOutcome,
    AttemptPhase,
    TaskState,
)


class StateTransitionError(Exception):
    """Raised when a CAS update finds the task in an unexpected state/version.

    This means either a concurrent writer (shouldn't happen: one worker, one
    task at a time) or a programming error calling the wrong transition. It is
    never an unknown *external* outcome -- those are represented as the
    TaskState.UNKNOWN state, not an exception.
    """


def _require_allowed(from_states: set[TaskState], to_state: TaskState) -> None:
    for from_state in from_states:
        if to_state not in ALLOWED_TRANSITIONS[from_state]:
            raise StateTransitionError(f"{from_state} -> {to_state} is not allowed")


def _cas(
    db: Database,
    session: Any,
    task_id: str,
    from_states: set[TaskState],
    to_state: TaskState,
    extra_set: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _require_allowed(from_states, to_state)
    update: dict[str, Any] = {
        "$set": {"state": to_state.value, "updated_at": now_iso(), **(extra_set or {})},
        "$inc": {"version": 1},
    }
    doc = db[TASKS].find_one_and_update(
        {"_id": task_id, "state": {"$in": [s.value for s in from_states]}},
        update,
        session=session,
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        raise StateTransitionError(
            f"task {task_id} was not in {from_states} when transitioning to {to_state}"
        )
    return doc


def record_intent(
    db: Database, task: dict[str, Any], request: dict[str, Any], worker_pid: int
) -> dict[str, Any]:
    """Invariant 1: persist the intent BEFORE sending any external call.

    Valid from PLANNED (first attempt) or FAILED_NOT_APPLIED (retry, same key).
    """
    task_id = task["_id"]
    next_attempt_no = task["attempt_count"] + 1

    def _do(session: Any) -> dict[str, Any]:
        updated = _cas(
            db,
            session,
            task_id,
            {TaskState.PLANNED, TaskState.FAILED_NOT_APPLIED},
            TaskState.INTENT_RECORDED,
            extra_set={"attempt_count": next_attempt_no},
        )
        db[ATTEMPTS].insert_one(
            {
                "_id": f"{task_id}:intent:{next_attempt_no}",
                "task_id": task_id,
                "run_id": task["run_id"],
                "attempt_no": next_attempt_no,
                "phase": AttemptPhase.INTENT.value,
                "idempotency_key": task["idempotency_key"],
                "request_hash": request_hash(request),
                "request": request,
                "worker_pid": worker_pid,
                "ts": now_iso(),
            },
            session=session,
        )
        return updated

    return db_module.run_in_transaction(db, _do)


def mark_sent(db: Database, task_id: str) -> dict[str, Any]:
    """Set SENT immediately before the network call fires. No second document
    is written here, so a plain CAS update is sufficient (already atomic)."""
    return _cas(db, None, task_id, {TaskState.INTENT_RECORDED}, TaskState.SENT)


def classify_send_result(
    *, timed_out: bool, connection_error: bool, http_status: int | None
) -> tuple[AttemptOutcome, TaskState]:
    """Pure classification of a send attempt's outcome -> next task state.

    Never swallows ambiguity: anything that isn't a clean, interpretable
    response becomes UNKNOWN, never a silent success or failure.
    """
    if timed_out:
        return AttemptOutcome.TIMEOUT, TaskState.UNKNOWN
    if connection_error or http_status is None:
        return AttemptOutcome.UNKNOWN, TaskState.UNKNOWN
    if 500 <= http_status < 600:
        return AttemptOutcome.ERROR, TaskState.UNKNOWN
    if 200 <= http_status < 300:
        return AttemptOutcome.ACKED, TaskState.VERIFYING
    # Any other definitive status (e.g. 4xx): the system told us something
    # concrete, so we know enough to move to VERIFYING and check by read.
    return AttemptOutcome.ERROR, TaskState.VERIFYING


def record_result(
    db: Database,
    task: dict[str, Any],
    attempt_no: int,
    outcome: AttemptOutcome,
    target_state: TaskState,
    http_status: int | None,
    response: dict[str, Any],
    error_signature: str | None,
) -> dict[str, Any]:
    """Record the result of a send attempt and transition SENT -> target_state."""
    task_id = task["_id"]

    def _do(session: Any) -> dict[str, Any]:
        updated = _cas(db, session, task_id, {TaskState.SENT}, target_state)
        db[ATTEMPTS].insert_one(
            {
                "_id": f"{task_id}:result:{attempt_no}",
                "task_id": task_id,
                "run_id": task["run_id"],
                "attempt_no": attempt_no,
                "phase": AttemptPhase.RESULT.value,
                "outcome": outcome.value,
                "http_status": http_status,
                "response": response,
                "error_signature": error_signature,
                "ts": now_iso(),
            },
            session=session,
        )
        return updated

    return db_module.run_in_transaction(db, _do)


def mark_verifying(db: Database, task_id: str) -> dict[str, Any]:
    """UNKNOWN -> VERIFYING: about to perform an external read."""
    return _cas(db, None, task_id, {TaskState.UNKNOWN}, TaskState.VERIFYING)


def ensure_verifying(db: Database, task: dict[str, Any]) -> dict[str, Any]:
    """Get a task into VERIFYING from whatever recoverable state it was found
    in (invariant 2: never retry SENT/UNKNOWN without verifying first; and
    CONTRACTS.md's stricter rule that INTENT_RECORDED is checked too)."""
    state = TaskState(task["state"])
    if state == TaskState.VERIFYING:
        return task
    if state in {TaskState.PLANNED, TaskState.INTENT_RECORDED, TaskState.SENT, TaskState.UNKNOWN}:
        return _cas(db, None, task["_id"], {state}, TaskState.VERIFYING)
    raise StateTransitionError(f"cannot verify from state {state}")


def resolve_verification(
    db: Database,
    task: dict[str, Any],
    effect_doc: dict[str, Any],
    target_state: TaskState,
) -> dict[str, Any]:
    """Record the external-read effect and transition VERIFYING -> target_state.

    target_state must be one of VERIFIED, FAILED_NOT_APPLIED, NEEDS_HUMAN
    (invariants 4 and 5: unverifiable or mismatched-money never auto-resolves
    to VERIFIED).
    """
    if target_state not in {
        TaskState.VERIFIED,
        TaskState.FAILED_NOT_APPLIED,
        TaskState.NEEDS_HUMAN,
    }:
        raise StateTransitionError(f"{target_state} is not a valid verification outcome")
    task_id = task["_id"]
    extra = {"verified_effect": effect_doc["observation"]} if target_state == TaskState.VERIFIED else {}

    def _do(session: Any) -> dict[str, Any]:
        updated = _cas(db, session, task_id, {TaskState.VERIFYING}, target_state, extra_set=extra)
        db[EFFECTS].insert_one({**effect_doc, "task_id": task_id}, session=session)
        return updated

    return db_module.run_in_transaction(db, _do)


def escalate(db: Database, task_id: str, reason: str) -> dict[str, Any]:
    """Explicit escalation to NEEDS_HUMAN from any state (invariant 4/5)."""
    current = db[TASKS].find_one({"_id": task_id})
    if current is None:
        raise StateTransitionError(f"task {task_id} not found")
    from_state = TaskState(current["state"])
    return _cas(db, None, task_id, {from_state}, TaskState.NEEDS_HUMAN, extra_set={"last_error": reason})

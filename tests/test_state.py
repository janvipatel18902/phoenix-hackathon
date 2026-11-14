from __future__ import annotations

import itertools

import pytest

from phoenix.db import ATTEMPTS, EFFECTS, TASKS
from phoenix.intents import idempotency_key
from phoenix.models import AttemptOutcome, AttemptPhase, TaskKind, TaskState
from phoenix.state import (
    StateTransitionError,
    classify_send_result,
    ensure_verifying,
    escalate,
    mark_sent,
    record_intent,
    record_result,
    resolve_verification,
)

RUN_ID = "run_test_001"
_seq_counter = itertools.count(1)


def _make_task(db, kind=TaskKind.REFUND, state=TaskState.PLANNED) -> dict:
    task_id = f"{RUN_ID}:{kind.value}:att_001"
    doc = {
        "_id": task_id,
        "run_id": RUN_ID,
        "seq": next(_seq_counter),
        "kind": kind.value,
        "system": "payments",
        "ref": "att_001",
        "state": state.value,
        "idempotency_key": idempotency_key("evt_concert_001", kind, "att_001"),
        "depends_on": [],
        "expected_effect": {"attendee_id": "att_001", "amount_minor": 8500, "currency": "CAD"},
        "verified_effect": None,
        "assumption": {},
        "attempt_count": 0,
        "adaptation_spec_id": None,
        "last_error": None,
        "version": 0,
        "created_at": "2026-11-14T00:00:00.000Z",
        "updated_at": "2026-11-14T00:00:00.000Z",
    }
    db[TASKS].insert_one(doc)
    return doc


# --- Invariant 1: persist the intent BEFORE sending any external call ---


def test_record_intent_writes_attempt_before_any_send(db):
    task = _make_task(db)
    updated = record_intent(db, task, {"amount": "85.00"}, worker_pid=4242)

    assert updated["state"] == TaskState.INTENT_RECORDED.value
    assert updated["attempt_count"] == 1

    attempt = db[ATTEMPTS].find_one({"task_id": task["_id"], "phase": AttemptPhase.INTENT.value})
    assert attempt is not None
    assert attempt["idempotency_key"] == task["idempotency_key"]
    assert attempt["request"] == {"amount": "85.00"}


# --- Invariant 6: state transition + attempt record in one transaction ---


def test_record_intent_and_attempt_are_written_together(db):
    task = _make_task(db)
    record_intent(db, task, {}, worker_pid=1)

    task_doc = db[TASKS].find_one({"_id": task["_id"]})
    attempt = db[ATTEMPTS].find_one({"task_id": task["_id"]})
    assert task_doc["state"] == TaskState.INTENT_RECORDED.value
    assert attempt is not None


def test_record_result_and_attempt_are_written_together(db):
    task = _make_task(db)
    record_intent(db, task, {}, worker_pid=1)
    task = db[TASKS].find_one({"_id": task["_id"]})
    mark_sent(db, task["_id"])
    task = db[TASKS].find_one({"_id": task["_id"]})

    record_result(
        db, task, 1, AttemptOutcome.ACKED, TaskState.VERIFYING, 201, {"refund_id": "r1"}, None
    )

    task_doc = db[TASKS].find_one({"_id": task["_id"]})
    result_attempt = db[ATTEMPTS].find_one(
        {"task_id": task["_id"], "phase": AttemptPhase.RESULT.value}
    )
    assert task_doc["state"] == TaskState.VERIFYING.value
    assert result_attempt["outcome"] == AttemptOutcome.ACKED.value


# --- Invariant 2: never retry SENT/UNKNOWN without verifying via external read first ---


def test_cannot_jump_from_sent_straight_to_verified():
    from phoenix.models import ALLOWED_TRANSITIONS

    # classify_send_result only ever lands SENT on UNKNOWN or VERIFYING...
    _, target = classify_send_result(timed_out=False, connection_error=False, http_status=200)
    assert target in {TaskState.UNKNOWN, TaskState.VERIFYING}
    # ...and the transition table has no SENT -> VERIFIED edge even if a
    # caller tried to force it.
    assert TaskState.VERIFIED not in ALLOWED_TRANSITIONS[TaskState.SENT]


def test_ensure_verifying_required_before_resolve(db):
    task = _make_task(db, state=TaskState.SENT)
    with pytest.raises(StateTransitionError):
        resolve_verification(
            db, task, {"_id": "e1", "observation": {}, "expected": {}, "matches_expected": False},
            TaskState.VERIFIED,
        )


def test_ensure_verifying_from_sent_and_unknown(db):
    sent_task = _make_task(db, state=TaskState.SENT)
    verifying = ensure_verifying(db, sent_task)
    assert verifying["state"] == TaskState.VERIFYING.value

    unknown_task = _make_task(db, kind=TaskKind.NOTIFY, state=TaskState.UNKNOWN)
    verifying2 = ensure_verifying(db, unknown_task)
    assert verifying2["state"] == TaskState.VERIFYING.value


# --- Invariant 3: only VERIFIED tasks count as progress (classify never lands on VERIFIED) ---


@pytest.mark.parametrize(
    "timed_out,connection_error,http_status,expected_state",
    [
        (True, False, None, TaskState.UNKNOWN),
        (False, True, None, TaskState.UNKNOWN),
        (False, False, 503, TaskState.UNKNOWN),
        (False, False, 201, TaskState.VERIFYING),
        (False, False, 422, TaskState.VERIFYING),
    ],
)
def test_classify_send_result_never_claims_verified(
    timed_out, connection_error, http_status, expected_state
):
    _, target = classify_send_result(
        timed_out=timed_out, connection_error=connection_error, http_status=http_status
    )
    assert target == expected_state
    assert target != TaskState.VERIFIED


# --- Invariant 4: unverifiable effect -> NEEDS_HUMAN, never a claimed success ---


def test_resolve_verification_rejects_a_non_verification_target(db):
    task = _make_task(db, state=TaskState.VERIFYING)
    effect_doc = {
        "_id": "e1",
        "observation": {},
        "expected": task["expected_effect"],
        "matches_expected": False,
    }
    with pytest.raises(StateTransitionError):
        resolve_verification(db, task, effect_doc, TaskState.SENT)


def test_cannot_resolve_verification_to_non_terminal_state(db):
    task = _make_task(db, state=TaskState.VERIFYING)
    with pytest.raises(StateTransitionError):
        resolve_verification(
            db, task, {"_id": "e1", "observation": {}, "expected": {}, "matches_expected": True},
            TaskState.UNKNOWN,
        )


# --- Invariant 5: applied-but-differs-from-expected => NEEDS_HUMAN, money never auto-corrected ---


def test_resolve_verification_needs_human_does_not_set_verified_effect(db):
    task = _make_task(db, state=TaskState.VERIFYING)
    effect_doc = {
        "_id": "e1",
        "observation": {"amount_minor": 999999},
        "expected": task["expected_effect"],
        "matches_expected": False,
    }
    updated = resolve_verification(db, task, effect_doc, TaskState.NEEDS_HUMAN)
    assert updated["state"] == TaskState.NEEDS_HUMAN.value
    assert updated.get("verified_effect") is None

    effect = db[EFFECTS].find_one({"_id": "e1"})
    assert effect is not None
    assert effect["matches_expected"] is False


def test_resolve_verification_verified_records_verified_effect(db):
    task = _make_task(db, state=TaskState.VERIFYING)
    expected = task["expected_effect"]
    effect_doc = {
        "_id": "e2",
        "observation": expected,
        "expected": expected,
        "matches_expected": True,
    }
    updated = resolve_verification(db, task, effect_doc, TaskState.VERIFIED)
    assert updated["state"] == TaskState.VERIFIED.value
    assert updated["verified_effect"] == expected


# --- escalate() is reachable from any state ---


def test_escalate_from_any_state(db):
    task = _make_task(db, state=TaskState.UNKNOWN)
    updated = escalate(db, task["_id"], "could not reach payments")
    assert updated["state"] == TaskState.NEEDS_HUMAN.value
    assert updated["last_error"] == "could not reach payments"


# --- FAILED_NOT_APPLIED -> INTENT_RECORDED: same-key retry, attempt_no + 1 ---


def test_retry_same_key_increments_attempt_count(db):
    task = _make_task(db, state=TaskState.FAILED_NOT_APPLIED)
    task["attempt_count"] = 1
    db[TASKS].update_one({"_id": task["_id"]}, {"$set": {"attempt_count": 1}})
    task = db[TASKS].find_one({"_id": task["_id"]})

    updated = record_intent(db, task, {}, worker_pid=1)

    assert updated["state"] == TaskState.INTENT_RECORDED.value
    assert updated["attempt_count"] == 2
    assert updated["idempotency_key"] == task["idempotency_key"]
    attempts = list(db[ATTEMPTS].find({"task_id": task["_id"]}))
    assert len(attempts) == 1
    assert attempts[0]["attempt_no"] == 2

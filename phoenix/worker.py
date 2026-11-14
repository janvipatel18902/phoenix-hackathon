"""The agent worker: builds the plan, processes tasks sequentially, heartbeats,
recovers on restart, and supports deterministic crash injection for demos.

Run: `python -m phoenix.worker`. Gets SIGKILL'd mid-task in demos; everything
it does is resumable because of the state machine in state.py/recover.py.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from typing import Any

import httpx
from bson import ObjectId
from pymongo.database import Database

from phoenix.db import EVENTS, RUNS, TASKS, init_db, now_iso
from phoenix.intents import idempotency_key
from phoenix.models import (
    EffectSource,
    EventLevel,
    EventSource,
    EventType,
    ExternalSystem,
    RunStatus,
    TaskKind,
    TaskState,
)
from phoenix.recover import build_effect_doc, decide_next_state, recover_task
from phoenix.state import (
    classify_send_result,
    ensure_verifying,
    escalate,
    mark_sent,
    record_intent,
    record_result,
    resolve_verification,
)
from phoenix.verify import verify_task

HEARTBEAT_INTERVAL_S = 2.0
MAX_ATTEMPTS = 5  # safety cap on same-key retries, avoids runaway recursion
NON_TERMINAL_RECOVERABLE = {
    TaskState.INTENT_RECORDED,
    TaskState.SENT,
    TaskState.UNKNOWN,
    TaskState.VERIFYING,
}


def task_delay_s() -> float:
    return float(os.environ.get("PHOENIX_TASK_DELAY_MS", "150")) / 1000.0


def _parse_crash_at() -> tuple[str, int] | None:
    raw = os.environ.get("PHOENIX_CRASH_AT")
    if not raw:
        return None
    point, _, seq_str = raw.partition(":")
    return point, int(seq_str)


def _maybe_crash(crash_at: tuple[str, int] | None, point: str, seq: int) -> None:
    if crash_at is not None and crash_at == (point, seq):
        os.kill(os.getpid(), signal.SIGKILL)


def emit_event(
    db: Database,
    run_id: str,
    level: EventLevel,
    type_: EventType,
    message: str,
    task_id: str | None = None,
    data: dict[str, Any] | None = None,
    source: EventSource = EventSource.WORKER,
) -> None:
    """Shared by worker.py, reconcile.py, and api/main.py (api emits its own
    events for e.g. WORKER_KILLED, CHAOS_APPLIED, STATE_WIPED -- pass
    source=EventSource.API or EventSource.CHAOS for those).

    _id is a bson ObjectId string: monotonically sortable by creation order,
    so GET /events's `Last-Event-ID` can resume a stream with a plain
    `_id > last_seen_id` query instead of needing a separate sequence counter.
    """
    db[EVENTS].insert_one(
        {
            "_id": str(ObjectId()),
            "ts": now_iso(),
            "run_id": run_id,
            "task_id": task_id,
            "level": level.value,
            "source": source.value,
            "type": type_.value,
            "message": message,
            "data": data or {},
        }
    )


# --- Planning (hard-coded fallback; CLAUDE.md: LLM plan-parsing is optional) ---


def build_plan(
    db: Database, client: httpx.Client, run_id: str, event_id: str, booking_id: str
) -> int:
    """Create PLANNED Task docs for every attendee plus one RELEASE_VENUE task.

    Deterministic, hard-coded plan: CANCEL_TICKET -> REFUND -> NOTIFY per
    attendee, then RELEASE_VENUE once all refunds are in. Idempotent: safe to
    call twice for the same run_id (unique index on (run_id, idempotency_key)
    makes a repeat insert a no-op failure we ignore).
    """
    resp = client.get(f"/ticketing/events/{event_id}/attendees")
    resp.raise_for_status()
    attendees = resp.json()["attendees"]

    docs: list[dict[str, Any]] = []
    refund_task_ids: list[str] = []
    seq = 0

    for attendee in attendees:
        att_id = attendee["attendee_id"]
        seq += 1
        cancel_id = f"{run_id}:{TaskKind.CANCEL_TICKET.value}:{att_id}"
        docs.append(
            _new_task(
                task_id=cancel_id,
                run_id=run_id,
                seq=seq,
                kind=TaskKind.CANCEL_TICKET,
                system=ExternalSystem.TICKETING,
                ref=att_id,
                idem_key=idempotency_key(event_id, TaskKind.CANCEL_TICKET, att_id),
                depends_on=[],
                expected_effect={
                    "ticket_id": attendee["ticket_id"],
                    "ticket_status": "cancelled",
                },
                assumption={"record_version": attendee["record_version"]},
            )
        )

        seq += 1
        refund_id = f"{run_id}:{TaskKind.REFUND.value}:{att_id}"
        refund_task_ids.append(refund_id)
        amount_minor = round(float(attendee["amount_paid"]) * 100)
        docs.append(
            _new_task(
                task_id=refund_id,
                run_id=run_id,
                seq=seq,
                kind=TaskKind.REFUND,
                system=ExternalSystem.PAYMENTS,
                ref=att_id,
                idem_key=idempotency_key(event_id, TaskKind.REFUND, att_id),
                depends_on=[cancel_id],
                expected_effect={
                    "attendee_id": att_id,
                    "amount_minor": amount_minor,
                    "currency": "CAD",
                },
                assumption={"record_version": attendee["record_version"]},
            )
        )

        seq += 1
        docs.append(
            _new_task(
                task_id=f"{run_id}:{TaskKind.NOTIFY.value}:{att_id}",
                run_id=run_id,
                seq=seq,
                kind=TaskKind.NOTIFY,
                system=ExternalSystem.NOTIFICATIONS,
                ref=att_id,
                idem_key=idempotency_key(event_id, TaskKind.NOTIFY, att_id),
                depends_on=[refund_id],
                expected_effect={
                    "reference": att_id,
                    "template": "event_cancelled",
                    "sent_count": 1,
                },
                assumption={"email": attendee["email"]},
            )
        )

    seq += 1
    docs.append(
        _new_task(
            task_id=f"{run_id}:{TaskKind.RELEASE_VENUE.value}:{booking_id}",
            run_id=run_id,
            seq=seq,
            kind=TaskKind.RELEASE_VENUE,
            system=ExternalSystem.VENUE,
            ref=booking_id,
            idem_key=idempotency_key(event_id, TaskKind.RELEASE_VENUE, booking_id),
            depends_on=refund_task_ids,
            expected_effect={"booking_id": booking_id, "status": "cancelled"},
            assumption={},
        )
    )

    if docs:
        db[TASKS].insert_many(docs, ordered=False)
    db[RUNS].update_one(
        {"_id": run_id}, {"$set": {"stats.total": len(docs), "updated_at": now_iso()}}
    )
    return len(docs)


def _new_task(
    *,
    task_id: str,
    run_id: str,
    seq: int,
    kind: TaskKind,
    system: ExternalSystem,
    ref: str,
    idem_key: str,
    depends_on: list[str],
    expected_effect: dict[str, Any],
    assumption: dict[str, Any],
) -> dict[str, Any]:
    ts = now_iso()
    return {
        "_id": task_id,
        "run_id": run_id,
        "seq": seq,
        "kind": kind.value,
        "system": system.value,
        "ref": ref,
        "state": TaskState.PLANNED.value,
        "idempotency_key": idem_key,
        "depends_on": depends_on,
        "expected_effect": expected_effect,
        "verified_effect": None,
        "assumption": assumption,
        "attempt_count": 0,
        "adaptation_spec_id": None,
        "last_error": None,
        "version": 0,
        "created_at": ts,
        "updated_at": ts,
    }


# --- Request bodies per task kind ---


def build_request(task: dict[str, Any]) -> dict[str, Any]:
    kind = TaskKind(task["kind"])
    expected = task["expected_effect"]
    if kind == TaskKind.CANCEL_TICKET:
        return {}
    if kind == TaskKind.REFUND:
        return {
            "attendee_id": expected["attendee_id"],
            "amount": f"{expected['amount_minor'] / 100:.2f}",
            "currency": expected["currency"],
        }
    if kind == TaskKind.NOTIFY:
        return {
            "to": task["assumption"]["email"],
            "template": expected["template"],
            "reference": expected["reference"],
        }
    if kind == TaskKind.RELEASE_VENUE:
        return {}
    raise ValueError(f"unknown task kind {kind}")


def send_request(
    client: httpx.Client, task: dict[str, Any], request: dict[str, Any]
) -> tuple[int | None, dict[str, Any], str | None, bool, bool]:
    """Returns (http_status, response_body, error_signature, timed_out, connection_error)."""
    kind = TaskKind(task["kind"])
    try:
        if kind == TaskKind.CANCEL_TICKET:
            resp = client.post(f"/ticketing/tickets/{task['expected_effect']['ticket_id']}/cancel")
        elif kind == TaskKind.REFUND:
            resp = client.post(
                "/payments/refunds",
                json=request,
                headers={"Idempotency-Key": task["idempotency_key"]},
            )
        elif kind == TaskKind.NOTIFY:
            resp = client.post("/notifications/emails", json=request)
        elif kind == TaskKind.RELEASE_VENUE:
            resp = client.post(f"/venue/bookings/{task['ref']}/cancel")
        else:
            raise ValueError(f"unknown task kind {kind}")
    except httpx.TimeoutException:
        return None, {}, "timeout", True, False
    except httpx.RequestError as exc:
        return None, {}, f"connection_error:{type(exc).__name__}", False, True

    try:
        body = resp.json()
    except ValueError:
        body = {}
    error_sig = None if resp.status_code < 400 else f"http_{resp.status_code}"
    return resp.status_code, body, error_sig, False, False


# --- Per-task pipeline ---


def process_task(
    db: Database,
    client: httpx.Client,
    task: dict[str, Any],
    worker_pid: int,
    crash_at: tuple[str, int] | None,
) -> dict[str, Any]:
    seq = task["seq"]

    if task["attempt_count"] >= MAX_ATTEMPTS:
        return escalate(db, task["_id"], f"gave up after {MAX_ATTEMPTS} attempts")

    _maybe_crash(crash_at, "before_intent", seq)
    request = build_request(task)
    task = record_intent(db, task, request, worker_pid)
    emit_event(db, task["run_id"], EventLevel.INFO, EventType.TASK_INTENT,
               f"Recorded intent for {task['kind']} {task['ref']}.", task_id=task["_id"])

    _maybe_crash(crash_at, "after_intent", seq)
    task = mark_sent(db, task["_id"])

    _maybe_crash(crash_at, "after_send", seq)
    http_status, response, error_sig, timed_out, conn_err = send_request(client, task, request)
    outcome, target_state = classify_send_result(
        timed_out=timed_out, connection_error=conn_err, http_status=http_status
    )

    _maybe_crash(crash_at, "after_response", seq)
    task = record_result(
        db, task, task["attempt_count"], outcome, target_state, http_status, response, error_sig
    )
    if target_state == TaskState.UNKNOWN:
        emit_event(db, task["run_id"], EventLevel.WARN, EventType.TASK_UNKNOWN,
                   f"{task['kind']} {task['ref']}: outcome unknown. Checking by external read.",
                   task_id=task["_id"])
    else:
        emit_event(db, task["run_id"], EventLevel.INFO, EventType.TASK_ACKED,
                   f"{task['kind']} {task['ref']}: got a response, verifying.", task_id=task["_id"])

    task = ensure_verifying(db, task)
    emit_event(db, task["run_id"], EventLevel.INFO, EventType.VERIFY_STARTED,
               f"Verifying {task['kind']} {task['ref']} against the real system.", task_id=task["_id"])
    read = verify_task(client, task)
    next_state = decide_next_state(read)

    _maybe_crash(crash_at, "after_verify", seq)
    effect_doc = build_effect_doc(task, EffectSource.VERIFY, read)
    task = resolve_verification(db, task, effect_doc, next_state)

    if next_state == TaskState.VERIFIED:
        db[RUNS].update_one({"_id": task["run_id"]}, {"$inc": {"stats.verified": 1}})
        emit_event(db, task["run_id"], EventLevel.INFO, EventType.VERIFIED,
                   f"{task['kind']} {task['ref']} verified.", task_id=task["_id"])
    elif next_state == TaskState.FAILED_NOT_APPLIED:
        emit_event(db, task["run_id"], EventLevel.WARN, EventType.NOT_APPLIED_RETRY,
                   f"{task['kind']} {task['ref']} not applied externally, retrying same key.",
                   task_id=task["_id"])
        return process_task(db, client, task, worker_pid, crash_at)
    else:
        emit_event(db, task["run_id"], EventLevel.ERROR, EventType.NEEDS_HUMAN,
                   f"{task['kind']} {task['ref']} needs a human: cannot verify or applied-but-differs.",
                   task_id=task["_id"])
    return task


def dependencies_satisfied(db: Database, task: dict[str, Any]) -> bool:
    if not task["depends_on"]:
        return True
    count = db[TASKS].count_documents(
        {"_id": {"$in": task["depends_on"]}, "state": TaskState.VERIFIED.value}
    )
    return count == len(task["depends_on"])


# --- Recovery and the main loop ---


def recover_run(db: Database, client: httpx.Client, run_id: str) -> None:
    """Invariant 2: never retry SENT/UNKNOWN without verifying first.
    Verify every task found in a non-terminal state before doing anything else."""
    cursor = db[TASKS].find(
        {"run_id": run_id, "state": {"$in": [s.value for s in NON_TERMINAL_RECOVERABLE]}}
    )
    for task in cursor:
        recover_task(db, client, task, EffectSource.RECONCILE)
        emit_event(db, run_id, EventLevel.INFO, EventType.WORKER_RESUMED,
                   f"Recovered {task['kind']} {task['ref']} from {task['state']} via external read.",
                   task_id=task["_id"])


def _heartbeat_loop(db: Database, run_id: str, stop: threading.Event, pid: int) -> None:
    while not stop.is_set():
        # Replace the whole subdocument, not dotted fields: `worker` starts as
        # null on a fresh run, and Mongo refuses to create a nested field
        # inside a null via a dotted $set.
        db[RUNS].update_one(
            {"_id": run_id}, {"$set": {"worker": {"pid": pid, "heartbeat_at": now_iso()}}}
        )
        stop.wait(HEARTBEAT_INTERVAL_S)


def run_worker_for(db: Database, client: httpx.Client, run_id: str) -> None:
    pid = os.getpid()
    crash_at = _parse_crash_at()

    run = db[RUNS].find_one({"_id": run_id})
    if run is None:
        raise ValueError(f"run {run_id} not found")

    if run["status"] == RunStatus.RUNNING.value:
        emit_event(db, run_id, EventLevel.INFO, EventType.WORKER_RESUMED,
                   "Worker resumed: checking what actually happened before continuing.")
        recover_run(db, client, run_id)
    else:
        if db[TASKS].count_documents({"run_id": run_id}) == 0:
            booking_id = run.get("assumptions", {}).get("booking_id", "book_001")
            n = build_plan(db, client, run_id, run["event_id"], booking_id)
            emit_event(db, run_id, EventLevel.INFO, EventType.PLAN_CREATED, f"Plan created: {n} tasks.")
        db[RUNS].update_one({"_id": run_id}, {"$set": {"status": RunStatus.RUNNING.value}})
        emit_event(db, run_id, EventLevel.INFO, EventType.RUN_STARTED, "Run started.")

    stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat_loop, args=(db, run_id, stop, pid), daemon=True)
    heartbeat.start()
    try:
        _process_remaining(db, client, run_id, pid, crash_at)
    finally:
        stop.set()


def _process_remaining(
    db: Database, client: httpx.Client, run_id: str, pid: int, crash_at: tuple[str, int] | None
) -> None:
    cursor = db[TASKS].find(
        {"run_id": run_id, "state": {"$in": [TaskState.PLANNED.value, TaskState.FAILED_NOT_APPLIED.value]}}
    ).sort("seq", 1)
    for task in cursor:
        task = db[TASKS].find_one({"_id": task["_id"]})  # re-read, state may have changed
        if task["state"] not in {TaskState.PLANNED.value, TaskState.FAILED_NOT_APPLIED.value}:
            continue
        if not dependencies_satisfied(db, task):
            escalate(db, task["_id"], "a dependency did not verify")
            continue
        process_task(db, client, task, pid, crash_at)
        time.sleep(task_delay_s())

    remaining_unverified = db[TASKS].count_documents(
        {"run_id": run_id, "state": {"$ne": TaskState.VERIFIED.value}}
    )
    status = RunStatus.NEEDS_HUMAN if remaining_unverified else RunStatus.COMPLETED
    db[RUNS].update_one(
        {"_id": run_id},
        {"$set": {"status": status.value, "completed_at": now_iso(), "updated_at": now_iso()}},
    )
    emit_event(db, run_id, EventLevel.INFO, EventType.RUN_COMPLETED,
               f"Run finished with status {status.value}.")


def main() -> None:
    db = init_db()
    client = httpx.Client(base_url=os.environ["MOCK_WORLD_URL"], timeout=3.0)
    run = db[RUNS].find_one({"status": {"$in": [RunStatus.RUNNING.value, RunStatus.PLANNING.value]}})
    if run is None:
        print("No active run found; nothing to do.")
        return
    run_worker_for(db, client, run["_id"])


if __name__ == "__main__":
    main()

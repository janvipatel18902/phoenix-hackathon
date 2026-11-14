"""Reconcile: rebuild from nothing after total state loss (the amnesia demo).

Recomputes every task's deterministic idempotency key and verifies it against
the external systems directly -- never assumes a freshly-recreated PLANNED
task is untouched, because the real effect may already exist from before the
wipe (contracts/CONTRACTS.md section 1).
"""

from __future__ import annotations

from typing import Any

import httpx
from pymongo.database import Database

from phoenix.db import RUNS, TASKS
from phoenix.models import EffectSource, EventLevel, EventType, TaskState
from phoenix.recover import recover_task
from phoenix.worker import build_plan, emit_event


def trigger_reconcile(db: Database, client: httpx.Client, run_id: str) -> dict[str, Any]:
    """Entry point for api/main.py's POST /runs/{run_id}/reconcile.

    Only needs run_id -- looks up event_id and booking_id from the run doc
    itself, the same way worker.py's run_worker_for does for build_plan.
    Synchronous: runs to completion and returns {"checked", "findings"}. The
    API can call this directly in a request handler, or hand it to a thread
    if it wants the HTTP response to return before reconcile finishes --
    that choice belongs to api/main.py, not here.
    """
    run = db[RUNS].find_one({"_id": run_id})
    if run is None:
        raise ValueError(f"run {run_id} not found")
    booking_id = run.get("assumptions", {}).get("booking_id", "book_001")
    return reconcile(db, client, run_id, run["event_id"], booking_id)


def reconcile(
    db: Database, client: httpx.Client, run_id: str, event_id: str, booking_id: str
) -> dict[str, Any]:
    emit_event(db, run_id, EventLevel.INFO, EventType.RECONCILE_STARTED,
               "Reconcile started: rebuilding from external truth.")

    existing = db[TASKS].count_documents({"run_id": run_id})
    if existing == 0:
        build_plan(db, client, run_id, event_id, booking_id)

    findings: list[dict[str, Any]] = []
    for task in list(db[TASKS].find({"run_id": run_id, "state": TaskState.PLANNED.value})):
        recovered = recover_task(db, client, task, EffectSource.RECONCILE)
        if recovered["state"] != TaskState.FAILED_NOT_APPLIED.value:
            # Genuinely new news: this "fresh" task already had a real effect.
            findings.append({"task_id": task["_id"], "found_state": recovered["state"]})
            emit_event(
                db, run_id, EventLevel.INFO, EventType.RECONCILE_FINDING,
                f"{task['kind']} {task['ref']}: already {recovered['state']} externally.",
                task_id=task["_id"],
            )

    checked = db[TASKS].count_documents({"run_id": run_id})
    return {"checked": checked, "findings": findings}

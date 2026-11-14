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

from phoenix.db import TASKS
from phoenix.models import EffectSource, EventLevel, EventType, TaskState
from phoenix.recover import recover_task
from phoenix.worker import build_plan, emit_event


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

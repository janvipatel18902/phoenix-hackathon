"""explain(): what was supposed to happen / what actually happened (verified
externally) / what is safe next. contracts/CONTRACTS.md section 7."""

from __future__ import annotations

from typing import Any

from pymongo.database import Database

from phoenix.db import EFFECTS, RUNS, TASKS
from phoenix.models import TaskState


def explain(db: Database, run_id: str) -> dict[str, Any]:
    run = db[RUNS].find_one({"_id": run_id})
    if run is None:
        raise ValueError(f"run {run_id} not found")

    total = db[TASKS].count_documents({"run_id": run_id})
    verified = db[TASKS].count_documents({"run_id": run_id, "state": TaskState.VERIFIED.value})
    unknown = db[TASKS].count_documents(
        {
            "run_id": run_id,
            "state": {
                "$in": [TaskState.SENT.value, TaskState.UNKNOWN.value, TaskState.VERIFYING.value]
            },
        }
    )
    needs_human_tasks = list(db[TASKS].find({"run_id": run_id, "state": TaskState.NEEDS_HUMAN.value}))

    evidence: list[dict[str, Any]] = []
    next_actions: list[dict[str, Any]] = []
    for task in needs_human_tasks:
        last_effect = db[EFFECTS].find_one({"task_id": task["_id"]}, sort=[("observed_at", -1)])
        observed = last_effect["observation"] if last_effect else {}
        evidence.append({"task_id": task["_id"], "observed": observed})
        next_actions.append(
            {
                "task_id": task["_id"],
                "action": "escalate to human",
                "reason": task.get("last_error") or "cannot verify external state",
            }
        )

    return {
        "supposed": {"plan_summary": run["instruction"], "total_tasks": total},
        "actual": {
            "verified": verified,
            "unknown": unknown,
            "needs_human": len(needs_human_tasks),
            "evidence": evidence,
        },
        "next": next_actions,
    }

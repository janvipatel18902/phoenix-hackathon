"""The recovery decision table (CLAUDE.md) and the orchestration that applies
it. Used both at worker restart (recovering every task found in a non-terminal
state) and inline after every normal send (CONTRACTS.md: "every task is
verified by an external read even after a clean 2xx")."""

from __future__ import annotations

from typing import Any

import httpx
from pymongo.database import Database

from phoenix.db import now_iso
from phoenix.models import EffectSource, TaskState
from phoenix.state import ensure_verifying, resolve_verification
from phoenix.verify import ExternalReadResult, verify_task


def decide_next_state(read: ExternalReadResult) -> TaskState:
    """CLAUDE.md recovery decision table:
    - cannot determine            -> NEEDS_HUMAN
    - not applied                 -> FAILED_NOT_APPLIED (caller retries, same key)
    - applied, matches expected   -> VERIFIED
    - applied, differs            -> NEEDS_HUMAN (never auto-correct money)
    """
    if read.applied is None:
        return TaskState.NEEDS_HUMAN
    if read.applied is False:
        return TaskState.FAILED_NOT_APPLIED
    if read.matches_expected is True:
        return TaskState.VERIFIED
    return TaskState.NEEDS_HUMAN


def build_effect_doc(
    task: dict[str, Any], source: EffectSource, read: ExternalReadResult
) -> dict[str, Any]:
    return {
        "_id": f"{task['_id']}:{source.value}:{task['attempt_count']}",
        "run_id": task["run_id"],
        "system": task["system"],
        "ref": task["ref"],
        "source": source.value,
        "observed_at": now_iso(),
        "observation": read.observation,
        "expected": task["expected_effect"],
        "matches_expected": bool(read.matches_expected),
    }


def recover_task(
    db: Database, client: httpx.Client, task: dict[str, Any], source: EffectSource
) -> dict[str, Any]:
    """Verify externally, then land the task on VERIFIED / FAILED_NOT_APPLIED /
    NEEDS_HUMAN. A FAILED_NOT_APPLIED result is not resent here -- the caller's
    normal per-task loop picks it up and retries with the same idempotency key,
    exactly like a fresh PLANNED task (record_intent accepts both source states).
    """
    verifying_task = ensure_verifying(db, task)
    read = verify_task(client, verifying_task)
    target = decide_next_state(read)
    effect_doc = build_effect_doc(verifying_task, source, read)
    return resolve_verification(db, verifying_task, effect_doc, target)

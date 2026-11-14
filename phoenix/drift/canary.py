"""Drift handling for the worker: detect, recall or diagnose, canary, activate.

Engine integration (CONTRACTS.md section 10):
  - before sending a REFUND:  request = adapt_request(db, task, request)
  - after a 422 is recorded:  outcome = resolve_drift(...); on ADAPTED the normal
    same-key retry rebuilds the request through adapt_request; on NEEDS_HUMAN
    the task is escalated.
Nothing here sends a real refund. The canary is a dry run (section 6).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from pymongo.database import Database

from phoenix.db import EFFECTS, RUNS, now_iso
from phoenix.drift import diagnose, memory
from phoenix.drift.spec import (
    AdaptationSpec,
    SpecContext,
    SpecError,
    apply_spec,
    spec_id,
    spec_to_dict,
    validate_spec,
)
from phoenix.models import EventLevel, EventType

MAX_PROPOSALS = 3
Emit = Callable[..., None]


@dataclass
class DriftOutcome:
    status: str  # "NOT_DRIFT" | "ADAPTED" | "NEEDS_HUMAN"
    reason: str
    spec_id: str | None = None
    memory_id: str | None = None


def _default_emit() -> Emit:
    # Imported lazily: worker.py will import this module, so a top-level import
    # of worker here would be circular.
    from phoenix.worker import emit_event

    return emit_event


def active_key(system: str, operation: str) -> str:
    return f"{system}:{operation}"


def active_adaptation(db: Database, run_id: str, system: str) -> dict[str, Any] | None:
    run = db[RUNS].find_one({"_id": run_id}, {"active_adaptations": 1}) or {}
    found = run.get("active_adaptations", {}).get(
        active_key(system, diagnose.OPERATIONS.get(system, ""))
    )
    return found if isinstance(found, dict) else None


def adapt_request(
    db: Database, task: dict[str, Any], request: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """Apply the run's active spec for this task's system, if any.
    Returns (request, spec_id or None). Raises SpecError if it cannot apply."""
    active = active_adaptation(db, task["run_id"], task["system"])
    if active is None:
        return request, None
    spec = AdaptationSpec.model_validate(active["spec"])
    return apply_spec(spec, request, task["expected_effect"]), active["spec_id"]


def dry_run_refund(
    client: httpx.Client, body: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """POST /payments/refunds?dry_run=true. Raises httpx.HTTPError if unreachable."""
    resp = client.post("/payments/refunds", params={"dry_run": "true"}, json=body)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {}


def _record_canary_effect(
    db: Database, task: dict[str, Any], observation: dict[str, Any], matches: bool
) -> None:
    db[EFFECTS].insert_one(
        {
            "_id": f"{task['_id']}:canary:{uuid.uuid4().hex[:8]}",
            "run_id": task["run_id"],
            "task_id": task["_id"],
            "system": task["system"],
            "ref": task["ref"],
            "source": "canary",
            "observed_at": now_iso(),
            "observation": observation,
            "expected": task["expected_effect"],
            "matches_expected": matches,
        }
    )


def _activate(
    db: Database,
    run_id: str,
    system: str,
    spec: AdaptationSpec,
    memory_id: str,
    to_version: int,
) -> str:
    sid = spec_id(spec)
    db[RUNS].update_one(
        {"_id": run_id},
        {
            "$set": {
                f"active_adaptations.{active_key(system, spec.operation)}": {
                    "spec_id": sid,
                    "memory_id": memory_id,
                    "spec": spec_to_dict(spec),
                },
                f"api_versions_seen.{system}": to_version,
                "updated_at": now_iso(),
            }
        },
    )
    return sid


def _bump(db: Database, run_id: str, **counters: int) -> None:
    db[RUNS].update_one(
        {"_id": run_id}, {"$inc": {f"stats.{k}": v for k, v in counters.items()}}
    )


def resolve_drift(
    db: Database,
    client: httpx.Client,
    task: dict[str, Any],
    request: dict[str, Any],
    http_status: int | None,
    response: dict[str, Any],
    emit: Emit | None = None,
) -> DriftOutcome:
    emit = emit or _default_emit()
    run_id, system = task["run_id"], task["system"]
    tid = task["_id"]
    if not diagnose.is_drift(system, http_status, response):
        return DriftOutcome("NOT_DRIFT", "not a recognised API change")
    assert http_status is not None
    if active_adaptation(db, run_id, system) is not None:
        return DriftOutcome(
            "NEEDS_HUMAN", "an adaptation is already active and was rejected again"
        )

    ctx = SpecContext(request, response["details"], set(task["expected_effect"]))
    from_version = int(
        db[RUNS]
        .find_one({"_id": run_id}, {"api_versions_seen": 1})
        .get("api_versions_seen", {})
        .get(system, 1)
    )
    signature = diagnose.build_signature(system, http_status, response, from_version)
    description = diagnose.describe_signature(system, signature)
    emit(
        db, run_id, EventLevel.WARN, EventType.DRIFT_DETECTED,
        f"The {system} API rejected our request ({description}). The API probably changed.",
        task_id=tid, data={"signature": signature},
    )  # fmt: skip

    rejected = _known_bad(db, system, description)
    feedback: list[str] = []
    candidates = _recall(db, emit, run_id, system, description, tid, rejected)
    for attempt in range(MAX_PROPOSALS):
        if candidates:
            raw, source_note, used_llm = (
                candidates.pop(0),
                "recalled from memory",
                False,
            )
        else:
            try:
                proposal = diagnose.propose_spec(
                    system, signature, ctx, rejected, feedback
                )
            except SpecError as exc:
                return _give_up(
                    db, emit, run_id, tid, f"could not propose an adaptation: {exc}"
                )
            raw, source_note, used_llm = proposal.spec, proposal.note, proposal.used_llm
            _bump(db, run_id, diagnosis_calls=1, llm_calls=1 if used_llm else 0)
        try:
            spec = validate_spec(raw, ctx)
        except SpecError as exc:
            rejected.append(raw)
            feedback.append(f"spec rejected before testing: {exc}")
            emit(db, run_id, EventLevel.WARN, EventType.ADAPTATION_PROPOSED,
                 f"A proposed fix was rejected before testing: {exc}", task_id=tid,
                 data={"spec": raw, "valid": False})  # fmt: skip
            continue
        if spec_to_dict(spec) in rejected:
            continue
        emit(db, run_id, EventLevel.INFO, EventType.ADAPTATION_PROPOSED,
             f"Proposed fix ({source_note}): {_ops_summary(spec)}.", task_id=tid,
             data={"spec": spec_to_dict(spec), "valid": True})  # fmt: skip
        verdict = _canary(db, client, emit, task, request, spec)
        if verdict is None:
            return _give_up(
                db, emit, run_id, tid, "could not reach payments to test the fix"
            )
        matches, observed = verdict
        result = {"matches_expected": matches, "observed": observed}
        if matches:
            mem_id, _ = memory.save_memory(
                db, run_id=run_id, system=system, signature=signature, description=description,
                spec=spec_to_dict(spec), canary_result=result, polarity="positive",
            )  # fmt: skip
            sid = _activate(
                db, run_id, system, spec, mem_id, signature["api_version_to"]
            )
            emit(db, run_id, EventLevel.INFO, EventType.MEMORY_SAVED,
                 "Saved what worked so the next run can reuse it (it will still be re-tested).",
                 task_id=tid, data={"memory_id": mem_id, "polarity": "positive"})  # fmt: skip
            return DriftOutcome("ADAPTED", "canary passed", sid, mem_id)
        rejected.append(spec_to_dict(spec))
        feedback.append(f"dry run of {spec_to_dict(spec)['ops']} returned {observed}")
        mem_id, _ = memory.save_memory(
            db, run_id=run_id, system=system, signature=signature, description=description,
            spec=spec_to_dict(spec), canary_result=result, polarity="negative",
        )  # fmt: skip
        emit(db, run_id, EventLevel.WARN, EventType.MEMORY_SAVED,
             "Remembered that this fix does NOT work, so it is never tried again.",
             task_id=tid, data={"memory_id": mem_id, "polarity": "negative"})  # fmt: skip
    return _give_up(
        db, emit, run_id, tid, f"no working fix after {MAX_PROPOSALS} proposals"
    )


def _ops_summary(spec: AdaptationSpec) -> str:
    return ", ".join(op.op.replace("_", " ") for op in spec.ops)


def _known_bad(db: Database, system: str, description: str) -> list[dict[str, Any]]:
    found = memory.retrieve(db, system, description, "negative")
    return [h.memory["adaptation_spec"] for h in found.hits]


def _recall(
    db: Database, emit: Emit, run_id: str, system: str, description: str, tid: str,
    rejected: list[dict[str, Any]],
) -> list[dict[str, Any]]:  # fmt: skip
    found = memory.retrieve(db, system, description, "positive")
    specs = [
        h.memory["adaptation_spec"]
        for h in found.hits
        if h.memory["adaptation_spec"] not in rejected
    ]
    if specs:
        best = found.hits[0]
        _bump(db, run_id, memory_hits=1)
        db[memory.RECOVERY_MEMORIES].update_one(
            {"_id": best.memory["_id"]}, {"$set": {"last_used_at": now_iso()}}
        )
        emit(db, run_id, EventLevel.INFO, EventType.MEMORY_RETRIEVED,
             f"Found a similar problem I solved before (match {best.score:.2f}). "
             "I will test it first rather than trust it.",
             task_id=tid, data={"memory_id": best.memory["_id"], "score": best.score, "via": found.via})  # fmt: skip
    return specs


def _canary(
    db: Database, client: httpx.Client, emit: Emit, task: dict[str, Any],
    request: dict[str, Any], spec: AdaptationSpec,
) -> tuple[bool, dict[str, Any]] | None:  # fmt: skip
    """Dry-run the adapted request. None means 'cannot determine' (no verdict)."""
    run_id, tid = task["run_id"], task["_id"]
    emit(db, run_id, EventLevel.INFO, EventType.CANARY_STARTED,
         f"Testing the fix on one refund ({task['ref']}) as a dry run. Nothing is sent for real.",
         task_id=tid)  # fmt: skip
    try:
        adapted = apply_spec(spec, request, task["expected_effect"])
        status, body = dry_run_refund(client, adapted)
    except (SpecError, httpx.HTTPError):
        return None
    if status == 200 and isinstance(body.get("would_store"), dict):
        observed = body["would_store"]
        matches = all(observed.get(k) == v for k, v in task["expected_effect"].items())
    else:
        observed, matches = {"http_status": status, "body": body}, False
    _record_canary_effect(db, task, observed, matches)
    if matches:
        emit(db, run_id, EventLevel.INFO, EventType.CANARY_PASSED,
             f"The dry run matches what we expect for {task['ref']}. Applying the fix to the batch.",
             task_id=tid, data={"observed": observed})  # fmt: skip
    else:
        emit(db, run_id, EventLevel.ERROR, EventType.CANARY_FAILED,
             f"The dry run would store {observed} instead of {task['expected_effect']}. "
             "Nothing was sent; the fix is rejected.",
             task_id=tid, data={"observed": observed})  # fmt: skip
    return matches, observed


def _give_up(
    db: Database, emit: Emit, run_id: str, tid: str, reason: str
) -> DriftOutcome:
    emit(db, run_id, EventLevel.ERROR, EventType.NEEDS_HUMAN,
         f"I could not safely adapt to the API change: {reason}.", task_id=tid)  # fmt: skip
    return DriftOutcome("NEEDS_HUMAN", reason)

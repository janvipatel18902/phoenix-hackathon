"""Pydantic document models matching contracts/CONTRACTS.md section 4 exactly."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _doc_model() -> ConfigDict:
    return ConfigDict(populate_by_name=True, extra="forbid")


class TaskState(str, Enum):
    PLANNED = "PLANNED"
    INTENT_RECORDED = "INTENT_RECORDED"
    SENT = "SENT"
    UNKNOWN = "UNKNOWN"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    FAILED_NOT_APPLIED = "FAILED_NOT_APPLIED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class TaskKind(str, Enum):
    CANCEL_TICKET = "CANCEL_TICKET"
    REFUND = "REFUND"
    NOTIFY = "NOTIFY"
    RELEASE_VENUE = "RELEASE_VENUE"


class ExternalSystem(str, Enum):
    TICKETING = "ticketing"
    PAYMENTS = "payments"
    NOTIFICATIONS = "notifications"
    VENUE = "venue"


class RunStatus(str, Enum):
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    ABORTED = "ABORTED"


class AttemptPhase(str, Enum):
    INTENT = "intent"
    RESULT = "result"


class AttemptOutcome(str, Enum):
    ACKED = "ACKED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"


class EffectSource(str, Enum):
    VERIFY = "verify"
    RECONCILE = "reconcile"
    CANARY = "canary"


class MemoryPolarity(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class EventLevel(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class EventSource(str, Enum):
    WORKER = "worker"
    API = "api"
    MOCK = "mock"
    CHAOS = "chaos"


class EventType(str, Enum):
    RUN_STARTED = "RUN_STARTED"
    PLAN_CREATED = "PLAN_CREATED"
    TASK_INTENT = "TASK_INTENT"
    TASK_SENT = "TASK_SENT"
    TASK_ACKED = "TASK_ACKED"
    TASK_UNKNOWN = "TASK_UNKNOWN"
    VERIFY_STARTED = "VERIFY_STARTED"
    VERIFIED = "VERIFIED"
    NOT_APPLIED_RETRY = "NOT_APPLIED_RETRY"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    WORKER_STARTED = "WORKER_STARTED"
    WORKER_RESUMED = "WORKER_RESUMED"
    WORKER_KILLED = "WORKER_KILLED"
    CHAOS_APPLIED = "CHAOS_APPLIED"
    DRIFT_DETECTED = "DRIFT_DETECTED"
    MEMORY_RETRIEVED = "MEMORY_RETRIEVED"
    ADAPTATION_PROPOSED = "ADAPTATION_PROPOSED"
    CANARY_STARTED = "CANARY_STARTED"
    CANARY_PASSED = "CANARY_PASSED"
    CANARY_FAILED = "CANARY_FAILED"
    MEMORY_SAVED = "MEMORY_SAVED"
    STATE_WIPED = "STATE_WIPED"
    RECONCILE_STARTED = "RECONCILE_STARTED"
    RECONCILE_FINDING = "RECONCILE_FINDING"
    RUN_COMPLETED = "RUN_COMPLETED"


# Allowed task state transitions, contracts/CONTRACTS.md section 3.
# INTENT_RECORDED -> VERIFYING and PLANNED -> VERIFYING are recovery/reconcile
# -only edges: CONTRACTS.md requires an external read before any resend even
# from INTENT_RECORDED (a prior attempt's response may have been dropped
# after commit yet actually applied), and reconcile rebuilds PLANNED tasks
# from scratch after state loss, so a "fresh" task may already have a real
# external effect from before the wipe. Neither edge is used by the normal
# first-time processing path in worker.py.
ALLOWED_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.PLANNED: {TaskState.INTENT_RECORDED, TaskState.VERIFYING},
    TaskState.INTENT_RECORDED: {TaskState.SENT, TaskState.VERIFYING},
    TaskState.SENT: {TaskState.UNKNOWN, TaskState.VERIFYING},
    TaskState.UNKNOWN: {TaskState.VERIFYING},
    TaskState.VERIFYING: {
        TaskState.VERIFIED,
        TaskState.FAILED_NOT_APPLIED,
        TaskState.NEEDS_HUMAN,
    },
    TaskState.FAILED_NOT_APPLIED: {TaskState.INTENT_RECORDED},
    TaskState.VERIFIED: set(),
    TaskState.NEEDS_HUMAN: set(),
}
# "any -> NEEDS_HUMAN" is an explicit escalation allowed from every state.
for _state in TaskState:
    ALLOWED_TRANSITIONS.setdefault(_state, set()).add(TaskState.NEEDS_HUMAN)


class WorkerInfo(BaseModel):
    model_config = _doc_model()

    pid: int
    heartbeat_at: str


class RunStats(BaseModel):
    model_config = _doc_model()

    total: int = 0
    verified: int = 0
    llm_calls: int = 0
    diagnosis_calls: int = 0
    memory_hits: int = 0


class Run(BaseModel):
    model_config = _doc_model()

    id: str = Field(alias="_id")
    instruction: str
    event_id: str
    status: RunStatus
    plan_fingerprint: str
    assumptions: dict[str, Any] = Field(default_factory=dict)
    worker: WorkerInfo | None = None
    stats: RunStats = Field(default_factory=RunStats)
    api_versions_seen: dict[str, int] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    completed_at: str | None = None


class Task(BaseModel):
    model_config = _doc_model()

    id: str = Field(alias="_id")
    run_id: str
    seq: int
    kind: TaskKind
    system: ExternalSystem
    ref: str
    state: TaskState
    idempotency_key: str
    depends_on: list[str] = Field(default_factory=list)
    expected_effect: dict[str, Any]
    verified_effect: dict[str, Any] | None = None
    assumption: dict[str, Any] = Field(default_factory=dict)
    attempt_count: int = 0
    adaptation_spec_id: str | None = None
    last_error: str | None = None
    version: int
    created_at: str
    updated_at: str


class AttemptIntent(BaseModel):
    """Attempt doc, phase='intent'. Append-only."""

    model_config = _doc_model()

    id: str = Field(alias="_id")
    task_id: str
    run_id: str
    attempt_no: int
    phase: Literal[AttemptPhase.INTENT] = AttemptPhase.INTENT
    idempotency_key: str
    request_hash: str
    request: dict[str, Any]
    worker_pid: int
    ts: str


class AttemptResult(BaseModel):
    """Attempt doc, phase='result'. Append-only."""

    model_config = _doc_model()

    id: str = Field(alias="_id")
    task_id: str
    run_id: str
    attempt_no: int
    phase: Literal[AttemptPhase.RESULT] = AttemptPhase.RESULT
    outcome: AttemptOutcome
    http_status: int | None = None
    response: dict[str, Any] = Field(default_factory=dict)
    error_signature: str | None = None
    ts: str


class Effect(BaseModel):
    model_config = _doc_model()

    id: str = Field(alias="_id")
    run_id: str
    task_id: str
    system: ExternalSystem
    ref: str
    source: EffectSource
    observed_at: str
    observation: dict[str, Any]
    expected: dict[str, Any]
    matches_expected: bool


class RecoveryMemory(BaseModel):
    model_config = _doc_model()

    id: str = Field(alias="_id")
    polarity: MemoryPolarity
    system: ExternalSystem
    signature: dict[str, Any]
    description: str
    embedding: list[float]
    adaptation_spec: dict[str, Any]
    canary_result: dict[str, Any]
    success_count: int = 0
    failure_count: int = 0
    source_run_id: str
    created_at: str
    last_used_at: str | None = None


class Event(BaseModel):
    model_config = _doc_model()

    id: str = Field(alias="_id")
    ts: str
    run_id: str
    task_id: str | None = None
    level: EventLevel
    source: EventSource
    type: EventType
    message: str
    data: dict[str, Any] = Field(default_factory=dict)

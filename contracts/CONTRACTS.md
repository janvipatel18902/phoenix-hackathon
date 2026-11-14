# CONTRACTS.md

Source of truth for shapes shared between Person A (engine) and Person B (mock world, API, UI, drift).
Rule: change a shape here FIRST, tell the other person, then change code. If this file conflicts with CLAUDE.md, this file wins on shapes; CLAUDE.md wins on process.

Conventions
- All timestamps: ISO 8601 UTC strings (`2026-11-14T15:04:05.123Z`).
- Money is held internally as integer minor units (cents) in `amount_minor`. Payments API v1 exposes dollars as a decimal STRING with 2 places (`"85.00"`), never a float.
- Ids are strings. Seed ids: event `evt_concert_001`, attendees `att_001`..`att_200`, booking `book_001`, currency `CAD`.
- Seed data: ALL 200 attendees paid `85.00` CAD (8500 minor units), so a full refund run totals 1,700,000 minor units. Seed `record_version` is 3 and the attendee-list `version` is 12.
- Ports: mock_world 8001, api 8000.
- External call timeout: 3 s (writes and verification reads).

---

## 1. Idempotency keys (deterministic on purpose)
`phx:{event_id}:{kind}:{ref}` e.g. `phx:evt_concert_001:REFUND:att_088`.
Derived from the event and the item, NOT from the run id or a random value. This lets `reconcile` recompute every key after state loss and look the effects up in the external systems.

---

## 2. Task kinds and expected effects (`expected_effect`)
| kind | system | ref | expected_effect |
|---|---|---|---|
| CANCEL_TICKET | ticketing | attendee_id | `{ "ticket_id": "tkt_088", "ticket_status": "cancelled" }` |
| REFUND | payments | attendee_id | `{ "attendee_id": "att_088", "amount_minor": 8500, "currency": "CAD" }` |
| NOTIFY | notifications | attendee_id | `{ "reference": "att_088", "template": "event_cancelled", "sent_count": 1 }` |
| RELEASE_VENUE | venue | booking_id | `{ "booking_id": "book_001", "status": "cancelled" }` |

Plan order per attendee: CANCEL_TICKET -> REFUND -> NOTIFY (NOTIFY `depends_on` the REFUND being VERIFIED). RELEASE_VENUE depends on ALL refunds VERIFIED. Worker processes tasks sequentially with `PHOENIX_TASK_DELAY_MS` (default 150) between tasks so demos are visible.

---

## 3. Task state machine
States: `PLANNED, INTENT_RECORDED, SENT, UNKNOWN, VERIFYING, VERIFIED, FAILED_NOT_APPLIED, NEEDS_HUMAN`

Allowed transitions
| from | to | note |
|---|---|---|
| PLANNED | INTENT_RECORDED | one Mongo transaction: task state + attempt `intent` doc |
| INTENT_RECORDED | SENT | set immediately before the network call |
| SENT | UNKNOWN | timeout, connection drop, 5xx |
| SENT | VERIFYING | got a response (2xx or definitive 4xx) |
| UNKNOWN | VERIFYING | |
| VERIFYING | VERIFIED | external read matches `expected_effect` |
| VERIFYING | FAILED_NOT_APPLIED | external read proves effect NOT applied |
| VERIFYING | NEEDS_HUMAN | cannot determine, or applied but differs from expected |
| FAILED_NOT_APPLIED | INTENT_RECORDED | retry with the SAME idempotency key, attempt_no + 1 |
| any | NEEDS_HUMAN | explicit escalation |
| INTENT_RECORDED | VERIFYING | recovery-only: a prior attempt's response may have been dropped after commit yet actually applied; must check before resending |
| PLANNED | VERIFYING | reconcile-only: after state loss, a freshly rebuilt task may already have a real external effect from before the wipe |

Every task is verified by an external read even after a clean 2xx. Only VERIFIED counts as progress.

Recovery on restart (stricter than CLAUDE.md): a task found in `INTENT_RECORDED`, `SENT`, `UNKNOWN` or `VERIFYING` may or may not have reached the external system. It is ALWAYS verified by an external read BEFORE any resend. The decision table in CLAUDE.md then applies to the read result.

Verification reads
| kind | read |
|---|---|
| CANCEL_TICKET | `GET /ticketing/attendees/{attendee_id}` -> `ticket_status` |
| REFUND | `GET /payments/refunds?idempotency_key=...` -> compare amount in minor units |
| NOTIFY | `GET /notifications/emails?reference=...` -> count |
| RELEASE_VENUE | `GET /venue/bookings/{booking_id}` -> `status` |

---

## 4. MongoDB collections (db `phoenix`)

### runs
```json
{
  "_id": "run_20261114_001",
  "instruction": "Cancel the event, refund every attendee, notify them, release the venue. Verify everything.",
  "event_id": "evt_concert_001",
  "status": "PLANNING | RUNNING | COMPLETED | NEEDS_HUMAN | ABORTED",
  "plan_fingerprint": "sha256:...",
  "assumptions": { "attendee_list_version": 12 },
  "worker": { "pid": 4242, "heartbeat_at": "..." },
  "stats": { "total": 601, "verified": 87, "llm_calls": 0, "diagnosis_calls": 0, "memory_hits": 0 },
  "api_versions_seen": { "payments": 1 },
  "created_at": "...", "updated_at": "...", "completed_at": null
}
```

### tasks
```json
{
  "_id": "run_20261114_001:REFUND:att_088",
  "run_id": "run_20261114_001",
  "seq": 262,
  "kind": "REFUND",
  "system": "payments",
  "ref": "att_088",
  "state": "UNKNOWN",
  "idempotency_key": "phx:evt_concert_001:REFUND:att_088",
  "depends_on": ["run_20261114_001:CANCEL_TICKET:att_088"],
  "expected_effect": { "attendee_id": "att_088", "amount_minor": 8500, "currency": "CAD" },
  "verified_effect": null,
  "assumption": { "record_version": 3 },
  "attempt_count": 1,
  "adaptation_spec_id": null,
  "last_error": null,
  "version": 4,
  "created_at": "...", "updated_at": "..."
}
```
`version` is an integer for optimistic concurrency; every transition increments it with a compare-and-set update.

### attempts (INSERT-ONLY)
Two phases per attempt, both inserted, never updated.
```json
{ "_id": "...", "task_id": "...", "run_id": "...", "attempt_no": 1, "phase": "intent",
  "idempotency_key": "...", "request_hash": "sha256:...", "request": { }, "worker_pid": 4242, "ts": "..." }
{ "_id": "...", "task_id": "...", "run_id": "...", "attempt_no": 1, "phase": "result",
  "outcome": "ACKED | ERROR | TIMEOUT | UNKNOWN", "http_status": 201, "response": { },
  "error_signature": null, "ts": "..." }
```

### effects
Facts read back from the external systems.
```json
{ "_id": "...", "run_id": "...", "task_id": "...", "system": "payments", "ref": "att_088",
  "source": "verify | reconcile | canary", "observed_at": "...",
  "observation": { }, "expected": { }, "matches_expected": true }
```

### recovery_memories
```json
{
  "_id": "mem_...",
  "polarity": "positive | negative",
  "system": "payments",
  "signature": { "operation": "create_refund", "http_status": 422,
                 "unknown_fields": ["amount"], "missing_fields": ["amount_minor", "reason_code"],
                 "api_version_from": 1, "api_version_to": 2 },
  "description": "payments create_refund returned 422: unknown field amount; missing fields amount_minor, reason_code",
  "embedding": [ ],
  "adaptation_spec": { },
  "canary_result": { "matches_expected": true },
  "success_count": 1, "failure_count": 0,
  "source_run_id": "...", "created_at": "...", "last_used_at": null
}
```
`description` is built deterministically from `signature` by code (stable retrieval), then embedded. Vector index: name `recovery_memories_vec`, path `embedding`, dimensions = `EMBEDDING_DIM`, similarity cosine, filter fields `system` and `polarity`. Retrieval threshold: configurable `MEMORY_MIN_SCORE` (start at 0.80, tune).

### events (feeds UI narration)
```json
{ "_id": "...", "ts": "...", "run_id": "...", "task_id": null,
  "level": "info | warn | error", "source": "worker | api | mock | chaos",
  "type": "TASK_UNKNOWN",
  "message": "Refund for att_088: outcome unknown (timeout). Checking payments by key.",
  "data": { } }
```
Event types: `RUN_STARTED, PLAN_CREATED, TASK_INTENT, TASK_SENT, TASK_ACKED, TASK_UNKNOWN, VERIFY_STARTED, VERIFIED, NOT_APPLIED_RETRY, NEEDS_HUMAN, WORKER_STARTED, WORKER_RESUMED, WORKER_KILLED, CHAOS_APPLIED, DRIFT_DETECTED, MEMORY_RETRIEVED, ADAPTATION_PROPOSED, CANARY_STARTED, CANARY_PASSED, CANARY_FAILED, MEMORY_SAVED, STATE_WIPED, RECONCILE_STARTED, RECONCILE_FINDING, RUN_COMPLETED`.
`message` is plain language a non-engineer can read on a projector.

### Indexes
- tasks: unique `(run_id, seq)`; unique `idempotency_key` per run (`(run_id, idempotency_key)`); `(run_id, state)`.
- attempts: `(task_id, attempt_no, phase)` unique.
- events: `(run_id, ts)`.
- Schema validation on `tasks` (state enum, required fields) and `attempts`.

---

## 5. Mock world HTTP API (port 8001)

### Ticketing
- `GET /ticketing/events/{event_id}/attendees` -> `{ "event_id", "version": 12, "attendees": [ { "attendee_id", "name", "email", "ticket_id", "ticket_status": "active|cancelled", "amount_paid": "85.00", "record_version": 3 } ] }`
- `GET /ticketing/attendees/{attendee_id}` -> single attendee object
- `POST /ticketing/tickets/{ticket_id}/cancel` -> `{ "ticket_id", "ticket_status": "cancelled" }` (naturally idempotent)

### Payments (API version 1, default)
- `POST /payments/refunds`  header `Idempotency-Key`
  body `{ "attendee_id": "att_088", "amount": "85.00", "currency": "CAD" }`
  -> `201 { "refund_id": "ref_...", "idempotency_key", "attendee_id", "amount": "85.00", "currency", "status": "succeeded", "created_at" }`
  Same key again -> `200` with the original body and header `Idempotent-Replay: true` (no second refund).
- `GET /payments/refunds?idempotency_key=...` or `?attendee_id=...` -> `{ "refunds": [ ... ] }`
- `GET /payments/refunds/{refund_id}`
- **Dry run:** `POST /payments/refunds?dry_run=true` validates the body under the CURRENT API version and returns `200 { "dry_run": true, "would_store": { "attendee_id", "amount_minor": 8500, "currency": "CAD" } }` (v2 adds `"reason_code"`) without committing anything: no refund, no idempotency-key entry, no `/admin/reality` change. `would_store` is always in minor units regardless of version, normalized from whatever the body said. An invalid body returns the SAME 422 as the real call. The `Idempotency-Key` header is not required and is ignored. Dry runs are exempt from chaos (latency, drop, fail).

### Payments (API version 2, after chaos `api_version`)
- `POST /payments/refunds` body `{ "attendee_id", "amount_minor": 8500, "currency": "CAD", "reason_code": "EVENT_CANCELLED" }`
- Old v1 body now returns `422 { "error": "validation_error", "details": [ { "field": "amount", "message": "unknown field" }, { "field": "amount_minor", "message": "required" }, { "field": "reason_code", "message": "required" } ] }`
- Responses and lookups use `amount_minor` (integer) instead of `amount`.
- `reason_code` enum: `EVENT_CANCELLED, CUSTOMER_REQUEST, DUPLICATE, FRAUD`.

### Notifications (no idempotency key; verify-before-resend)
- `POST /notifications/emails` body `{ "to", "template": "event_cancelled", "reference": "att_088" }` -> `201 { "message_id" }`
- `GET /notifications/emails?reference=att_088` -> `{ "emails": [ { "message_id", "to", "template", "reference", "sent_at" } ] }`

### Venue (one-shot, NOT idempotent)
- `POST /venue/bookings/{booking_id}/cancel` -> first call `200 { "booking_id", "status": "cancelled", "fee_minor": 25000 }`; any later call `409 { "error": "already_cancelled" }`
- `GET /venue/bookings/{booking_id}` -> `{ "booking_id", "status": "active|cancelled" }`

### Admin and chaos
Chaos `latency`, `drop_response_after_commit` and `fail_before_commit` apply to committing POST requests ONLY. GETs (including verification reads and `/admin/*`) are never delayed, failed or dropped, and neither is `?dry_run=true`. Order for a committing POST: latency, then validation (a 422 never consumes a fail/drop count), then `fail_before_commit` (500, not applied), then commit, then `drop_response_after_commit` (the commit stands, the handler sleeps past the client's 3 s timeout, `MOCK_DROP_SLEEP_S`, default 3.5). A drop only fires when something was actually committed. `nth: k` means "fire once, on the k-th matching write" (count is ignored); otherwise `count` consecutive matching writes fire. `POST /admin/chaos/clear` removes all drop/fail/latency rules and resets payments to API v1 (mutated records are not undone; use `/admin/reset`).
- `POST /admin/reset` body `{ "event_id": "evt_concert_001", "attendees": 200, "seed": 1 }` -> reseeds everything (all attendees paid `85.00`; `seed` is recorded only), clears chaos, payments back to API v1.
- `GET /admin/reality` -> `{ "payments": { "refund_count": 88, "total_minor": 748000, "duplicate_refunds": 0 }, "ticketing": { "cancelled": 88 }, "notifications": { "sent": 87 }, "venue": { "status": "active" }, "payments_api_version": 1 }`
  `duplicate_refunds` counts attendees refunded more than once (the double-refund detector).
- `GET /admin/chaos` -> current chaos state.
- `POST /admin/chaos/drop_response_after_commit` body `{ "system": "payments", "count": 1, "nth": null }` -> the next matching write is APPLIED but the connection is dropped (client sees timeout).
- `POST /admin/chaos/fail_before_commit` body `{ "system": "payments", "count": 1 }` -> returns 500 and does NOT apply.
- `POST /admin/chaos/latency` body `{ "system": "payments", "ms": 5000, "count": null }`
- `POST /admin/chaos/api_version` body `{ "system": "payments", "version": 2 }`
- `POST /admin/chaos/mutate_record` body `{ "attendee_id": "att_120", "fields": { "email": "new@example.com" } }` -> also bumps `record_version` and the event attendee-list `version`.
- `POST /admin/chaos/clear`

---

## 6. Adaptation spec (declarative, validated, applied by code)
```json
{
  "spec_version": 1,
  "system": "payments",
  "operation": "create_refund",
  "ops": [
    { "op": "rename_field", "from": "amount", "to": "amount_minor" },
    { "op": "scale_value", "field": "amount_minor", "factor": 100, "from_type": "decimal_string", "to_type": "integer", "rounding": "half_up" },
    { "op": "add_field", "field": "reason_code", "value": { "source": "constant", "value": "EVENT_CANCELLED" } }
  ]
}
```
Allowed ops ONLY: `rename_field`, `scale_value`, `add_field`, `remove_field`. `add_field` value sources: `constant`, `task_field` (name of a task expected_effect field). The validator rejects unknown ops, unknown sources, non-numeric or out-of-range factors, and fields that do not appear in the error details or request schema.

Canary: apply the spec to the first affected REFUND and DRY-RUN it (`POST /payments/refunds?dry_run=true`, section 5). Compare `would_store` to the task's `expected_effect`.
- Mismatch: save a `negative` memory, record an `effects` doc with `source: canary` and `matches_expected: false`, emit `CANARY_FAILED`. NOTHING is sent for real, so no money moves.
- Match: send that task for real through the normal path (intent, send, external-read verify), emit `CANARY_PASSED`, then continue the batch and save or reinforce the memory.
The demo's wrong first proposal is the spec above WITHOUT the `scale_value` op (it would store 85 minor units instead of 8500); the dry run must catch it.

---

## 7. API service (port 8000)
- `POST /runs` body `{ "instruction": "...", "event_id": "evt_concert_001" }` -> `201 { "run_id" }`
- `GET /runs/{run_id}` -> run doc plus `state_counts`.
- `GET /runs/{run_id}/grid` -> `{ "cells": [ { "attendee_id": "att_001", "state": "VERIFIED" } ] }` (200 cells; each cell shows the least-advanced state among that attendee's tasks).
- `GET /runs/{run_id}/tasks?state=UNKNOWN`
- `GET /runs/{run_id}/explain` ->
```json
{ "supposed": { "plan_summary": "...", "total_tasks": 601 },
  "actual":   { "verified": 600, "unknown": 0, "needs_human": 1, "evidence": [ { "task_id": "...", "observed": { } } ] },
  "next":     [ { "task_id": "...", "action": "escalate to human", "reason": "applied but amount differs" } ] }
```
- `GET /events?run_id=...` Server-Sent Events (`text/event-stream`); each `data:` line is one `events` document; supports `Last-Event-ID` with the event `_id` order.
- `GET /reality` -> `{ "belief": { "verified_refunds": 87, "verified_minor": 739500 }, "reality": { ...mock /admin/reality... } }`
- `POST /chaos/{name}` -> proxies to the mock `/admin/chaos/{name}`.
- `POST /agent/kill` -> SIGKILL the worker, returns `{ "pid" }`; emits `WORKER_KILLED`.
- `POST /agent/start` -> spawns the worker; returns `{ "pid" }`.
- `GET /agent/status` -> `{ "alive": true, "pid": 4242, "heartbeat_age_s": 1.2 }`
- `POST /runs/{run_id}/wipe_state` -> deletes that run's tasks/attempts/effects (the amnesia demo); emits `STATE_WIPED`.
- `POST /runs/{run_id}/reconcile` -> starts reconcile (the worker runs it).
- `GET /learning/stats` -> `{ "runs": [ { "run_id", "event_id", "diagnosis_calls": 1, "memory_hits": 0, "duration_s": 41.2 } ] }`
- `POST /reset` -> resets mock world, clears this app's collections, keeps `recovery_memories` unless `?keep_memories=false`.

---

## 8. Worker behavior
- Heartbeat: update `runs.worker.heartbeat_at` every 2 s.
- On start: find runs with `status = RUNNING`, emit `WORKER_RESUMED`, run recovery (verify uncertain tasks), then continue.
- Crash injection for deterministic tests and demos: env `PHOENIX_CRASH_AT=<point>:<n>` where point is one of `before_intent, after_intent, after_send, after_response, after_verify` and `n` is the task `seq` (kills the process with SIGKILL semantics, no cleanup).
- Concurrency: one task at a time (keeps ordering and demos simple).

---

## 9. Environment variables (`.env`, gitignored)
```
MONGODB_URI=
MONGODB_DB=phoenix
MOCK_WORLD_URL=http://localhost:8001
LLM_PROVIDER=
LLM_MODEL=
LLM_API_KEY=
EMBEDDING_PROVIDER=
EMBEDDING_MODEL=
EMBEDDING_API_KEY=
EMBEDDING_DIM=
MEMORY_MIN_SCORE=0.80
PHOENIX_TASK_DELAY_MS=150
PHOENIX_CRASH_AT=
```

---

## 10. Change log
(Add one line per change: date/time, who, what, which section.)
- 2026-11-14, Person A, moved this file from repo root to contracts/CONTRACTS.md to match CLAUDE.md's layout; added two recovery/reconcile-only transitions (INTENT_RECORDED -> VERIFYING, PLANNED -> VERIFYING) needed to implement the "always verify before resend" rule in section 3 and the amnesia-reconcile flow; section 3.
- 2026-11-14, Person B, seed: all 200 attendees paid 85.00 CAD (record_version 3, list version 12); Conventions and section 5 (`/admin/reset`).
- 2026-11-14, Person B, chaos latency/drop/fail apply to committing POST requests only; GETs and dry runs are never affected; `clear` also resets payments to v1; defined `nth` and the fail/drop ordering; section 5 (Admin and chaos).
- 2026-11-14, Person B, added payments dry run `POST /payments/refunds?dry_run=true` returning `{dry_run, would_store}` without committing; section 5 (Payments).
- 2026-11-14, Person B, canary now dry-runs the first affected REFUND, compares `would_store` to `expected_effect`, and only sends for real on a match; a mismatch saves a negative memory and sends nothing; section 6. (Resolves the conflict with invariant 5.)

# Phoenix

Hackathon project (MongoDB Agents in Context, Problem 3: Finish the Job). Built in one day by 2 people. Python backend.
One line: an agent that knows what ACTUALLY happened across external systems and finishes the job after crashes, timeouts, state loss and API changes.

## Scenario
Event-cancellation agent: cancel an event, refund 200 attendees, notify them, release the venue, verify everything.
Four MOCK external systems in a separate process (`mock_world`): ticketing, payments (idempotency keys, lookup by key), notifications, venue (one-shot, not idempotent).
Chaos endpoints on the mock world: drop_response_after_commit, fail_before_commit, latency, api_version switch (payments `amount` dollars -> `amount_minor` cents + required `reason_code`), mutate_record.

## Architecture
- Processes: `mock_world` (FastAPI), `phoenix_worker` (the agent; gets kill -9'd in demos), `api` (FastAPI, serves UI + SSE), MongoDB Atlas (required sandbox).
- Deterministic core (plain Python, NO LangGraph/Temporal): state machine, write-ahead intents, idempotency, external verification, resume, reconcile.
- LLM used ONLY to: parse instruction into a plan (hard-coded fallback exists), diagnose unknown errors, propose a declarative adaptation spec. The LLM never executes actions.
- Mongo collections: runs, tasks, attempts (append-only), effects, recovery_memories (embedding + Atlas Vector Search), events.

## Task state machine
PLANNED -> INTENT_RECORDED -> SENT -> VERIFYING -> VERIFIED. Side states: UNKNOWN, FAILED_NOT_APPLIED, NEEDS_HUMAN.

Recovery decision table (on restart):
| Found | External read says | Action |
|---|---|---|
| INTENT_RECORDED, never sent | not applied | send (same key) |
| SENT or UNKNOWN | applied, matches expected | mark VERIFIED, do not resend |
| SENT or UNKNOWN | applied, differs from expected | NEEDS_HUMAN |
| SENT or UNKNOWN | not applied | retry with same key |
| any | cannot determine | NEEDS_HUMAN |

## Invariants (never violate; add a test for each)
1. Persist the intent (with idempotency key) BEFORE sending any external call.
2. Never retry a task in SENT/UNKNOWN without verifying via an external read first.
3. Only VERIFIED tasks count as progress.
4. If an effect cannot be verified, go to NEEDS_HUMAN. Never claim success.
5. Applied-but-differs-from-expected => NEEDS_HUMAN (never auto-correct money).
6. State transition + attempt record are written in one Mongo transaction.
7. Adaptation specs are declarative (rename/scale/add_field only), validated, applied by code, and ALWAYS canary-verified on one task before the batch. Retrieved memories are never trusted without the canary.

## Layout (keep it flat; do not add layers)
- contracts/CONTRACTS.md  document shapes, states, endpoints, event shapes (source of truth)
- mock_world/             mock systems + chaos
- phoenix/                engine: db.py models.py state.py intents.py verify.py recover.py reconcile.py explain.py worker.py ; drift/: diagnose.py spec.py canary.py memory.py
- api/                    main.py (REST + SSE + control endpoints)
- ui/                     index.html app.js style.css (static, no build step, NO CDN, no frameworks)
- scripts/                reset.sh, seed
- tests/                  pytest, for invariants and the recovery decision table

## Ownership (2 people; avoid merge conflicts)
- Person A: phoenix/ engine (everything except drift/), tests/.
- Person B: mock_world/, api/, ui/, phoenix/drift/, scripts/.
- Change shared shapes only by editing contracts/CONTRACTS.md first, and tell the other person.

## Coding standards
- Python 3.11+, type hints everywhere, pydantic models for documents, `ruff` clean.
- Small, single-purpose functions. No global mutable state. No clever metaprogramming.
- Handle errors explicitly. No bare `except`. Never swallow an exception that could hide an unknown outcome: an unknown outcome must become state UNKNOWN, not a success or a plain failure.
- External calls: always set timeouts. Always send the idempotency key where the system supports it.
- Database writes that must be atomic use Mongo transactions. Operations that can be repeated (seeding, index creation, recovery) must be idempotent.
- Config via environment variables (`.env`, gitignored). NEVER commit secrets or API keys.
- Every meaningful step emits an `events` doc in plain language (the UI narrates from these).
- Comments explain WHY when non-obvious; code should explain WHAT.
- Prefer the simplest design that makes the demo correct. No speculative abstractions, plugin systems or extra config layers.
- Do not rewrite or reformat working code unrelated to the task.

## How to work
- Before editing, read the contracts file and every file you will change or that your change depends on, so your code matches what exists.
- For multi-file or tricky changes, state a short plan first, including assumptions, then implement.
- For anything touching external calls or state transitions, list the possible crash points (before intent, after intent, after send, after response, before verification, after verification) and confirm each is recoverable under the decision table.
- Verify your work: run the relevant tests, and run the process when feasible. Do not say something works unless you ran it. Report what you actually ran and the result.
- Never weaken an invariant or a test to make something pass. If an invariant seems wrong, flag it and stop.
- If the contracts are ambiguous or two requirements conflict, say so and propose the simplest resolution before building on a guess.
- Do not touch files outside your ownership area; if you need a change there, say what is needed.

## Run
- `scripts/reset.sh` reseeds mock world + Mongo
- `uvicorn mock_world.app:app --port 8001`
- `python -m phoenix.worker`
- `uvicorn api.main:app --port 8000`
- `pytest -q tests/`

## Hackathon rules to respect
- Everything is built during the event. Public repo. Atlas Sandbox only.
- The UI is a demo surface for the agent (grid, Belief-vs-Reality panel, narration feed, chaos buttons). NOT a dashboard. No charts or analytics pages.
- Hard gates: kill/resume works by 2:00 PM; ambiguous-outcome handling by 3:00 PM; feature freeze 4:15 PM.

## Definition of done
Kill at any point and restart -> no duplicate refund, correct final count. Ambiguous refund resolved by external read. Drift fixed via canary and stored in recovery_memories, reused in run 2. `explain` prints: what was supposed to happen / what actually happened (verified externally) / what is safe next.

"""API service (CONTRACTS.md section 7): REST + SSE + control endpoints, and the
static UI. Run: `uvicorn api.main:app --port 8000`.

The API never executes agent actions. It reads Mongo, proxies chaos to the mock
world, and starts/kills the worker process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import httpx
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError

from phoenix.db import (
    ATTEMPTS,
    EFFECTS,
    EVENTS,
    RECOVERY_MEMORIES,
    RUNS,
    TASKS,
    init_db,
    now_iso,
)
from phoenix.explain import explain
from phoenix.models import (
    EventLevel,
    EventSource,
    EventType,
    Run,
    RunStatus,
    TaskKind,
    TaskState,
)
from phoenix.reconcile import trigger_reconcile
from phoenix.worker import emit_event

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_DIR = REPO_ROOT / "ui"
DEFAULT_INSTRUCTION = "Cancel the event, refund every attendee, notify them, release the venue. Verify everything."
ACTIVE = [RunStatus.RUNNING.value, RunStatus.PLANNING.value]
HEARTBEAT_ALIVE_S = 6.0
SSE_POLL_S = 0.3
SSE_LOOKBACK_S = (
    3  # ObjectIds from different processes are not strictly ordered within a second
)
MAX_TASKS_RETURNED = 1000
CHAOS_NAMES = {
    "drop_response_after_commit",
    "fail_before_commit",
    "latency",
    "api_version",
    "mutate_record",
    "clear",
}
# Grid cell priority: attention states win, otherwise the least-advanced state.
PROGRESS = [
    TaskState.PLANNED,
    TaskState.FAILED_NOT_APPLIED,
    TaskState.INTENT_RECORDED,
    TaskState.SENT,
    TaskState.VERIFYING,
    TaskState.VERIFIED,
]
Spawner = Callable[[dict[str, str]], "subprocess.Popen[bytes]"]


def default_spawner(env: dict[str, str]) -> subprocess.Popen[bytes]:
    (REPO_ROOT / "logs").mkdir(exist_ok=True)
    log = open(REPO_ROOT / "logs" / "worker.log", "ab")  # noqa: SIM115 - handed to the child
    return subprocess.Popen(
        [sys.executable, "-m", "phoenix.worker"],
        cwd=REPO_ROOT,
        env=env,
        stdout=log,
        stderr=log,
        start_new_session=True,  # a Ctrl-C on the API must not take the worker with it
    )


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class NoCacheStaticFiles(StaticFiles):
    """A browser must never run a stale UI during a demo."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store"
        return response


class RunRequest(BaseModel):
    instruction: str = DEFAULT_INSTRUCTION
    event_id: str = "evt_concert_001"


class StartRequest(BaseModel):
    crash_at: str | None = None  # e.g. "after_send:262", see CONTRACTS.md section 8
    demo_wrong_first: bool = False  # PHOENIX_DEMO_WRONG_FIRST for the canary demo


def create_app(
    db: Database | None = None,
    mock_client: httpx.Client | None = None,
    spawner: Spawner | None = None,
) -> FastAPI:
    app = FastAPI(title="Phoenix API")
    mock = mock_client or httpx.Client(
        base_url=os.environ.get("MOCK_WORLD_URL", "http://localhost:8001"), timeout=3.0
    )
    spawn = spawner or default_spawner
    db_lock = threading.Lock()
    holder: dict[str, Any] = {"db": db, "proc": None, "reconcile": {}}

    def get_db() -> Database:
        with db_lock:
            if holder["db"] is None:
                holder["db"] = init_db()
            return holder["db"]  # type: ignore[no-any-return]

    app.state.holder = holder

    # --- helpers ---

    def latest_run(d: Database, run_id: str | None = None) -> dict[str, Any] | None:
        if run_id:
            return d[RUNS].find_one({"_id": run_id})
        return d[RUNS].find_one(sort=[("created_at", -1)])

    def require_run(d: Database, run_id: str) -> dict[str, Any]:
        run = d[RUNS].find_one({"_id": run_id})
        if run is None:
            raise HTTPException(404, f"run {run_id} not found")
        return run

    def active_run(d: Database) -> dict[str, Any] | None:
        return d[RUNS].find_one({"status": {"$in": ACTIVE}}, sort=[("created_at", -1)])

    def event_run_id(d: Database) -> str:
        run = latest_run(d)
        return str(run["_id"]) if run else "global"

    def api_event(
        d: Database,
        run_id: str,
        type_: EventType,
        message: str,
        *,
        source: EventSource = EventSource.API,
        level: EventLevel = EventLevel.INFO,
        data: dict[str, Any] | None = None,
    ) -> None:
        emit_event(d, run_id, level, type_, message, data=data, source=source)

    def worker_pid(d: Database) -> int | None:
        """The live worker's pid, from our own child handle or the run heartbeat."""
        proc = holder["proc"]
        if proc is not None and proc.poll() is None:
            return int(proc.pid)
        run = active_run(d)
        info = (run or {}).get("worker") or {}
        if info.get("pid") and info.get("heartbeat_at"):
            age = (datetime.now(UTC) - parse_ts(info["heartbeat_at"])).total_seconds()
            if age < HEARTBEAT_ALIVE_S and pid_alive(int(info["pid"])):
                return int(info["pid"])
        return None

    def mock_get(path: str) -> dict[str, Any]:
        try:
            resp = mock.get(path)
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                502, f"mock world unavailable: {type(exc).__name__}"
            ) from exc

    # --- runs ---

    @app.post("/runs", status_code=201)
    def create_run(body: RunRequest) -> dict[str, str]:
        d = get_db()
        if active_run(d) is not None:
            raise HTTPException(409, "a run is already active; finish or /reset first")
        attendees = mock_get(f"/ticketing/events/{body.event_id}/attendees")
        version = attendees["version"]
        day = datetime.now(UTC).strftime("%Y%m%d")
        fingerprint = hashlib.sha256(
            f"{body.instruction}|{body.event_id}|{version}".encode()
        ).hexdigest()
        n = d[RUNS].count_documents({"_id": {"$regex": f"^run_{day}_"}})
        for _ in range(10):
            n += 1
            run_id = f"run_{day}_{n:03d}"
            ts = now_iso()
            doc = Run(
                _id=run_id,
                instruction=body.instruction,
                event_id=body.event_id,
                status=RunStatus.PLANNING,
                plan_fingerprint=f"sha256:{fingerprint}",
                assumptions={
                    "attendee_list_version": version,
                    "booking_id": "book_001",
                },
                api_versions_seen={"payments": 1},
                created_at=ts,
                updated_at=ts,
            ).model_dump(
                by_alias=True, exclude={"worker"}
            )  # worker is $set by the heartbeat; null breaks it
            try:
                d[RUNS].insert_one(doc)
            except DuplicateKeyError:
                continue
            return {"run_id": run_id}
        raise HTTPException(500, "could not allocate a run id")

    @app.get("/runs")
    def list_runs() -> dict[str, Any]:
        d = get_db()
        runs = list(d[RUNS].find().sort("created_at", -1).limit(20))
        return {"runs": runs}

    def state_counts(d: Database, run_id: str) -> dict[str, int]:
        rows = d[TASKS].aggregate(
            [
                {"$match": {"run_id": run_id}},
                {"$group": {"_id": "$state", "n": {"$sum": 1}}},
            ]
        )
        counts = {s.value: 0 for s in TaskState}
        counts.update({r["_id"]: r["n"] for r in rows})
        return counts

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        d = get_db()
        run = require_run(d, run_id)
        counts = state_counts(d, run_id)
        run["state_counts"] = counts
        run["stats"] = {
            **run.get("stats", {}),
            "verified": counts[TaskState.VERIFIED.value],
        }
        return run

    @app.get("/runs/{run_id}/grid")
    def grid(run_id: str) -> dict[str, Any]:
        d = get_db()
        require_run(d, run_id)
        states: dict[str, list[str]] = {}
        venue = None
        for t in d[TASKS].find({"run_id": run_id}, {"ref": 1, "state": 1, "kind": 1}):
            if t["kind"] == TaskKind.RELEASE_VENUE.value:
                venue = t["state"]
            else:
                states.setdefault(t["ref"], []).append(t["state"])
        cells = [
            {"attendee_id": att, "state": cell_state(sts)}
            for att, sts in sorted(states.items())
        ]
        return {"cells": cells, "venue": venue}

    def cell_state(task_states: list[str]) -> str:
        for attention in (TaskState.NEEDS_HUMAN, TaskState.UNKNOWN):
            if attention.value in task_states:
                return attention.value
        known = [s for s in PROGRESS if s.value in task_states]
        return known[0].value if known else task_states[0]

    @app.get("/runs/{run_id}/tasks")
    def list_tasks(
        run_id: str,
        state: str | None = None,
        kind: str | None = None,
        limit: int = Query(200, ge=1, le=MAX_TASKS_RETURNED),
    ) -> dict[str, Any]:
        d = get_db()
        require_run(d, run_id)
        query: dict[str, Any] = {"run_id": run_id}
        if state:
            query["state"] = state
        if kind:
            query["kind"] = kind
        return {"tasks": list(d[TASKS].find(query).sort("seq", 1).limit(limit))}

    @app.get("/runs/{run_id}/explain")
    def explain_run(run_id: str) -> dict[str, Any]:
        d = get_db()
        require_run(d, run_id)
        return explain(d, run_id)

    @app.get("/runs/{run_id}/attempts/{task_id:path}")
    def task_attempts(run_id: str, task_id: str) -> dict[str, Any]:
        d = get_db()
        found = d[ATTEMPTS].find({"run_id": run_id, "task_id": task_id}).sort("ts", 1)
        return {"attempts": list(found)}

    # --- events (SSE) ---

    def lower_bound(cursor: str | None) -> str | None:
        """Re-read a little before the cursor so a later-written event with a
        smaller ObjectId (different process, same second) is not skipped."""
        if cursor is None:
            return None
        try:
            floor = ObjectId(cursor).generation_time - timedelta(seconds=SSE_LOOKBACK_S)
        except InvalidId:
            return cursor
        return str(ObjectId.from_datetime(floor))

    def fetch_events(
        d: Database, run_id: str | None, floor: str | None, tail: int
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if run_id:
            query["run_id"] = run_id
        if floor is not None:
            query["_id"] = {"$gte": floor}
            return list(d[EVENTS].find(query).sort("_id", 1).limit(500))
        newest = list(d[EVENTS].find(query).sort("_id", -1).limit(tail))
        return newest[::-1]

    def sse_frame(event: dict[str, Any]) -> str:
        return f"id: {event['_id']}\ndata: {json.dumps(event, default=str)}\n\n"

    @app.get("/events")
    async def events(
        request: Request,
        run_id: str | None = None,
        tail: int = Query(100, ge=0, le=500),
    ) -> StreamingResponse:
        d = get_db()
        last_id = request.headers.get("last-event-id")

        async def stream() -> AsyncIterator[str]:
            cursor = last_id
            seen: set[str] = set()
            yield "retry: 1000\n\n"
            idle = 0.0
            first = True
            while not await request.is_disconnected():
                floor = lower_bound(cursor)
                batch = await run_in_threadpool(
                    fetch_events, d, run_id, floor, tail if first else 0
                )
                first = False
                sent = False
                for event in batch:
                    eid = str(event["_id"])
                    if eid in seen:
                        continue
                    seen.add(eid)
                    cursor = eid if cursor is None or eid > cursor else cursor
                    sent = True
                    yield sse_frame(event)
                if len(seen) > 5000:
                    seen = {
                        s for s in seen if cursor and s >= (lower_bound(cursor) or "")
                    }
                idle = 0.0 if sent else idle + SSE_POLL_S
                if idle >= 15:
                    yield ": keep-alive\n\n"
                    idle = 0.0
                await asyncio.sleep(SSE_POLL_S)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- belief vs reality ---

    @app.get("/reality")
    def reality(run_id: str | None = None) -> dict[str, Any]:
        d = get_db()
        run = latest_run(d, run_id)
        belief = {
            "verified_refunds": 0,
            "verified_minor": 0,
            "verified_cancellations": 0,
            "verified_notifications": 0,
            "venue_released": False,
        }
        if run is not None:
            for t in d[TASKS].find(
                {"run_id": run["_id"], "state": TaskState.VERIFIED.value},
                {"kind": 1, "expected_effect": 1},
            ):
                kind = t["kind"]
                if kind == TaskKind.REFUND.value:
                    belief["verified_refunds"] += 1
                    belief["verified_minor"] += int(
                        t["expected_effect"]["amount_minor"]
                    )
                elif kind == TaskKind.CANCEL_TICKET.value:
                    belief["verified_cancellations"] += 1
                elif kind == TaskKind.NOTIFY.value:
                    belief["verified_notifications"] += 1
                elif kind == TaskKind.RELEASE_VENUE.value:
                    belief["venue_released"] = True
        try:
            real: dict[str, Any] | None = mock_get("/admin/reality")
            error = None
        except HTTPException as exc:
            real, error = None, exc.detail
        return {
            "run_id": run["_id"] if run else None,
            "belief": belief,
            "reality": real,
            "reality_error": error,
        }

    # --- chaos ---

    def chaos_message(name: str, body: dict[str, Any]) -> str:
        system = body.get("system", "payments")
        return {
            "drop_response_after_commit": f"Chaos: the next {system} write will be APPLIED but its response lost.",
            "fail_before_commit": f"Chaos: the next {system} write will fail before it is applied.",
            "latency": f"Chaos: {system} writes are now slow ({body.get('ms')} ms).",
            "api_version": f"Chaos: the {system} API just changed to version {body.get('version')}.",
            "mutate_record": f"Chaos: attendee {body.get('attendee_id')} was edited behind the agent's back.",
            "clear": "Chaos cleared: the mock world behaves normally again.",
        }[name]

    @app.get("/chaos")
    def chaos_state() -> dict[str, Any]:
        return mock_get("/admin/chaos")

    @app.post("/chaos/{name}")
    def chaos(
        name: str, body: Annotated[dict[str, Any] | None, Body()] = None
    ) -> JSONResponse:
        if name not in CHAOS_NAMES:
            raise HTTPException(404, f"unknown chaos {name!r}")
        d = get_db()
        payload = body or {}
        try:
            resp = mock.post(f"/admin/chaos/{name}", json=payload)
        except httpx.HTTPError as exc:
            raise HTTPException(
                502, f"mock world unavailable: {type(exc).__name__}"
            ) from exc
        if resp.status_code < 300:
            api_event(
                d, event_run_id(d), EventType.CHAOS_APPLIED, chaos_message(name, payload),
                source=EventSource.CHAOS, level=EventLevel.WARN, data={"name": name, "body": payload},
            )  # fmt: skip
        return JSONResponse(resp.json(), status_code=resp.status_code)

    # --- agent control ---

    @app.get("/agent/status")
    def agent_status() -> dict[str, Any]:
        d = get_db()
        pid = worker_pid(d)
        run = active_run(d) or latest_run(d)
        beat = ((run or {}).get("worker") or {}).get("heartbeat_at")
        age = (
            round((datetime.now(UTC) - parse_ts(beat)).total_seconds(), 1)
            if beat
            else None
        )
        return {"alive": pid is not None, "pid": pid, "heartbeat_age_s": age}

    @app.post("/agent/start")
    def agent_start(
        body: Annotated[StartRequest | None, Body()] = None,
    ) -> dict[str, Any]:
        d = get_db()
        run = active_run(d)
        if run is None:
            raise HTTPException(409, "no active run to work on; POST /runs first")
        existing = worker_pid(d)
        if existing is not None:
            raise HTTPException(409, f"worker already running (pid {existing})")
        opts = body or StartRequest()
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        # The worker requires MOCK_WORLD_URL; default it to the mock we proxy to.
        env.setdefault("MOCK_WORLD_URL", str(mock.base_url).rstrip("/"))
        env.pop("PHOENIX_CRASH_AT", None)
        env.pop("PHOENIX_DEMO_WRONG_FIRST", None)
        if opts.crash_at:
            env["PHOENIX_CRASH_AT"] = opts.crash_at
        if opts.demo_wrong_first:
            env["PHOENIX_DEMO_WRONG_FIRST"] = "1"
        holder["proc"] = spawn(env)
        pid = int(holder["proc"].pid)
        resuming = run["status"] == RunStatus.RUNNING.value
        api_event(
            d, run["_id"], EventType.WORKER_STARTED,
            f"Agent process started (pid {pid})" + (" to resume the run." if resuming else "."),
            data={"pid": pid, "crash_at": opts.crash_at},
        )  # fmt: skip
        return {"pid": pid}

    @app.post("/agent/kill")
    def agent_kill() -> dict[str, Any]:
        d = get_db()
        pid = worker_pid(d)
        if pid is None:
            raise HTTPException(409, "no live worker to kill")
        os.kill(pid, signal.SIGKILL)
        proc = holder["proc"]
        if proc is not None and proc.pid == pid:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        api_event(
            d, event_run_id(d), EventType.WORKER_KILLED,
            f"The agent process (pid {pid}) was killed without warning (kill -9).",
            level=EventLevel.WARN, data={"pid": pid},
        )  # fmt: skip
        return {"pid": pid}

    # --- amnesia demo ---

    @app.post("/runs/{run_id}/wipe_state")
    def wipe_state(run_id: str) -> dict[str, Any]:
        d = get_db()
        require_run(d, run_id)
        if worker_pid(d) is not None:
            raise HTTPException(
                409, "kill the agent first: wiping state under a live worker"
            )
        deleted = {
            name: d[name].delete_many({"run_id": run_id}).deleted_count
            for name in (TASKS, ATTEMPTS, EFFECTS)
        }
        d[RUNS].update_one(
            {"_id": run_id},
            {"$set": {"stats.total": 0, "stats.verified": 0, "updated_at": now_iso()},
             "$unset": {"active_adaptations": ""}},
        )  # fmt: skip
        api_event(
            d, run_id, EventType.STATE_WIPED,
            "The agent's memory of this run was wiped: tasks, attempts and effects are gone.",
            level=EventLevel.WARN, data=deleted,
        )  # fmt: skip
        return {"deleted": deleted}

    @app.post("/runs/{run_id}/reconcile", status_code=202)
    def reconcile_run(run_id: str, start_worker: bool = True) -> dict[str, Any]:
        d = get_db()
        require_run(d, run_id)
        if worker_pid(d) is not None:
            raise HTTPException(
                409, "kill the agent first: reconcile needs the run to itself"
            )
        if holder["reconcile"].get("running"):
            raise HTTPException(409, "reconcile already running")
        holder["reconcile"] = {
            "run_id": run_id,
            "running": True,
            "result": None,
            "error": None,
        }

        def work() -> None:
            state = holder["reconcile"]
            try:
                state["result"] = trigger_reconcile(d, mock, run_id)
                if start_worker and active_run(d) is not None:
                    agent_start(None)
            except (PyMongoError, httpx.HTTPError, ValueError, HTTPException) as exc:
                state["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                state["running"] = False

        threading.Thread(target=work, daemon=True).start()
        return {"started": True}

    @app.get("/runs/{run_id}/reconcile")
    def reconcile_status(run_id: str) -> dict[str, Any]:
        state = holder["reconcile"]
        return state if state.get("run_id") == run_id else {"running": False}

    # --- learning, memories, reset ---

    @app.get("/learning/stats")
    def learning_stats() -> dict[str, Any]:
        d = get_db()
        rows = []
        for run in d[RUNS].find().sort("created_at", 1):
            end = (
                parse_ts(run["completed_at"])
                if run.get("completed_at")
                else datetime.now(UTC)
            )
            stats = run.get("stats", {})
            rows.append(
                {
                    "run_id": run["_id"],
                    "event_id": run["event_id"],
                    "diagnosis_calls": stats.get("diagnosis_calls", 0),
                    "memory_hits": stats.get("memory_hits", 0),
                    "duration_s": round(
                        (end - parse_ts(run["created_at"])).total_seconds(), 1
                    ),
                }
            )
        return {"runs": rows}

    @app.get("/memories")
    def memories() -> dict[str, Any]:
        d = get_db()
        docs = d[RECOVERY_MEMORIES].find({}, {"embedding": 0}).sort("created_at", 1)
        return {"memories": list(docs)}

    @app.post("/reset")
    def reset(
        keep_memories: bool = True, attendees: int = Query(200, ge=1, le=200)
    ) -> dict[str, Any]:
        d = get_db()
        pid = worker_pid(d)
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
        try:
            resp = mock.post(
                "/admin/reset",
                json={"event_id": "evt_concert_001", "attendees": attendees, "seed": 1},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(
                502, f"mock world unavailable: {type(exc).__name__}"
            ) from exc
        names = [RUNS, TASKS, ATTEMPTS, EFFECTS, EVENTS]
        if not keep_memories:
            names.append(RECOVERY_MEMORIES)
        cleared = {n: d[n].delete_many({}).deleted_count for n in names}
        holder["reconcile"] = {}
        return {"cleared": cleared, "kept_memories": keep_memories}

    if UI_DIR.exists():
        app.mount("/", NoCacheStaticFiles(directory=UI_DIR, html=True), name="ui")
    return app


app = create_app()

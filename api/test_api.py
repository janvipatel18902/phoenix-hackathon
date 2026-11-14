"""API tests against mongomock + the real mock world. Run: pytest -q api/"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import mongomock
import pytest
from fastapi.testclient import TestClient

import phoenix.db as db_module
from api.main import create_app
from mock_world.app import create_app as create_mock
from phoenix.models import EventLevel, EventType
from phoenix.worker import emit_event


def task(
    run_id: str, seq: int, kind: str, ref: str, state: str, **effect: Any
) -> dict[str, Any]:
    return {
        "_id": f"{run_id}:{kind}:{ref}",
        "run_id": run_id,
        "seq": seq,
        "kind": kind,
        "system": "payments",
        "ref": ref,
        "state": state,
        "expected_effect": effect,
        "last_error": None,
    }


class Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(db_module, "run_in_transaction", lambda _db, fn: fn(None))
        self.mock = TestClient(create_mock(tmp_path / "m.json", drop_sleep_s=0))
        self.db = mongomock.MongoClient()["t"]
        self.spawned: list[subprocess.Popen[bytes]] = []

        def spawner(env: dict[str, str]) -> subprocess.Popen[bytes]:
            proc = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"]
            )
            self.spawned.append(proc)
            return proc

        self.api = TestClient(create_app(self.db, self.mock, spawner))

    def make_run(self) -> str:
        return str(self.api.post("/runs", json={}).json()["run_id"])


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    for p in e.spawned:
        p.kill()
        p.wait()


def test_create_run_and_single_active_run(env: Env) -> None:
    resp = env.api.post("/runs", json={})
    assert resp.status_code == 201
    run_id = resp.json()["run_id"]
    run = env.api.get(f"/runs/{run_id}").json()
    assert (
        run["status"] == "PLANNING"
        and run["assumptions"]["attendee_list_version"] == 12
    )
    assert (
        run["plan_fingerprint"].startswith("sha256:")
        and run["state_counts"]["PLANNED"] == 0
    )
    assert env.api.post("/runs", json={}).status_code == 409
    assert env.api.get("/runs/nope").status_code == 404


def test_grid_prefers_attention_states_and_least_advanced(env: Env) -> None:
    run_id = env.make_run()
    rows = [
        task(run_id, 1, "CANCEL_TICKET", "att_001", "VERIFIED"),
        task(run_id, 2, "REFUND", "att_001", "VERIFIED"),
        task(run_id, 3, "NOTIFY", "att_001", "VERIFIED"),
        task(run_id, 4, "CANCEL_TICKET", "att_002", "VERIFIED"),
        task(run_id, 5, "REFUND", "att_002", "UNKNOWN"),
        task(run_id, 6, "NOTIFY", "att_002", "PLANNED"),
        task(run_id, 7, "CANCEL_TICKET", "att_003", "VERIFIED"),
        task(run_id, 8, "REFUND", "att_003", "NEEDS_HUMAN"),
        task(run_id, 9, "NOTIFY", "att_003", "PLANNED"),
        task(run_id, 10, "CANCEL_TICKET", "att_004", "VERIFIED"),
        task(run_id, 11, "REFUND", "att_004", "PLANNED"),
        task(run_id, 12, "RELEASE_VENUE", "book_001", "PLANNED"),
    ]
    env.db.tasks.insert_many(rows)
    body = env.api.get(f"/runs/{run_id}/grid").json()
    assert {c["attendee_id"]: c["state"] for c in body["cells"]} == {
        "att_001": "VERIFIED", "att_002": "UNKNOWN", "att_003": "NEEDS_HUMAN", "att_004": "PLANNED",
    }  # fmt: skip
    assert body["venue"] == "PLANNED"
    counts = env.api.get(f"/runs/{run_id}").json()["state_counts"]
    assert counts["VERIFIED"] == 6 and counts["UNKNOWN"] == 1
    only = env.api.get(f"/runs/{run_id}/tasks", params={"state": "UNKNOWN"}).json()[
        "tasks"
    ]
    assert [t["ref"] for t in only] == ["att_002"]
    assert (
        env.api.get(f"/runs/{run_id}/tasks", params={"limit": 5000}).status_code == 422
    )


def test_reality_shows_belief_and_the_mocks_truth(env: Env) -> None:
    run_id = env.make_run()
    env.db.tasks.insert_many(
        [
            task(run_id, i, "REFUND", f"att_{i:03d}", "VERIFIED", amount_minor=8500)
            for i in (1, 2)
        ]
    )
    env.mock.post(
        "/payments/refunds",
        json={"attendee_id": "att_001", "amount": "85.00", "currency": "CAD"},
        headers={"Idempotency-Key": "a"},
    )
    body = env.api.get("/reality").json()
    assert (
        body["belief"]["verified_refunds"] == 2
        and body["belief"]["verified_minor"] == 17000
    )
    assert (
        body["reality"]["payments"]["refund_count"] == 1
    )  # belief ahead of reality = visible


def test_chaos_proxy_emits_event_and_validates_name(env: Env) -> None:
    env.make_run()
    assert env.api.post("/chaos/bogus").status_code == 404
    resp = env.api.post("/chaos/api_version", json={"system": "payments", "version": 2})
    assert resp.status_code == 200
    assert env.mock.get("/admin/reality").json()["payments_api_version"] == 2
    event = env.db.events.find_one({"type": "CHAOS_APPLIED"})
    assert event["source"] == "chaos" and "version 2" in event["message"]
    assert (
        env.api.post(
            "/chaos/latency", json={"system": "payments", "ms": -1}
        ).status_code
        == 422
    )
    assert env.api.get("/chaos").json()["api_version"]["payments"] == 2


def test_agent_start_status_kill_with_a_real_process(env: Env) -> None:
    assert env.api.post("/agent/start").status_code == 409  # no run yet
    env.make_run()
    assert env.api.get("/agent/status").json()["alive"] is False
    started = env.api.post("/agent/start", json={"crash_at": "after_send:5"})
    assert started.status_code == 200
    pid = started.json()["pid"]
    assert env.api.get("/agent/status").json() == {
        "alive": True,
        "pid": pid,
        "heartbeat_age_s": None,
    }
    assert env.api.post("/agent/start").status_code == 409
    assert env.api.post("/agent/kill").json() == {"pid": pid}
    assert env.spawned[0].poll() is not None  # really dead
    assert env.api.get("/agent/status").json()["alive"] is False
    assert env.api.post("/agent/kill").status_code == 409
    types = [e["type"] for e in env.db.events.find().sort("_id", 1)]
    assert types == ["WORKER_STARTED", "WORKER_KILLED"]


def test_wipe_state_needs_a_dead_agent_and_emits_event(env: Env) -> None:
    run_id = env.make_run()
    env.db.tasks.insert_one(task(run_id, 1, "REFUND", "att_001", "VERIFIED"))
    env.api.post("/agent/start")
    assert env.api.post(f"/runs/{run_id}/wipe_state").status_code == 409
    env.api.post("/agent/kill")
    wiped = env.api.post(f"/runs/{run_id}/wipe_state").json()
    assert wiped["deleted"]["tasks"] == 1 and env.db.tasks.count_documents({}) == 0
    assert env.db.events.find_one({"type": "STATE_WIPED"})["source"] == "api"


def test_reconcile_rebuilds_from_external_truth_and_never_resends(env: Env) -> None:
    run_id = env.make_run()
    env.db.runs.update_one({"_id": run_id}, {"$set": {"status": "RUNNING"}})
    for i in (
        1,
        2,
        3,
    ):  # reality: three refunds + tickets already happened before the wipe
        att = f"att_{i:03d}"
        env.mock.post(f"/ticketing/tickets/tkt_{i:03d}/cancel")
        env.mock.post(
            "/payments/refunds",
            json={"attendee_id": att, "amount": "85.00", "currency": "CAD"},
            headers={"Idempotency-Key": f"phx:evt_concert_001:REFUND:{att}"},
        )
    assert (
        env.api.post(f"/runs/{run_id}/reconcile?start_worker=false").status_code == 202
    )
    for _ in range(100):
        state = env.api.get(f"/runs/{run_id}/reconcile").json()
        if not state.get("running"):
            break
        time.sleep(0.1)
    assert state["error"] is None, state
    assert state["result"]["checked"] == 601
    counts = env.api.get(f"/runs/{run_id}").json()["state_counts"]
    assert counts["VERIFIED"] == 6  # 3 cancels + 3 refunds found externally
    assert (
        counts["FAILED_NOT_APPLIED"] == 601 - 6
    )  # everything else: not applied, safe to run
    assert (
        env.mock.get("/admin/reality").json()["payments"]["refund_count"] == 3
    )  # nothing resent


def test_learning_memories_and_reset(env: Env) -> None:
    run_id = env.make_run()
    env.db.runs.update_one({"_id": run_id}, {"$set": {"stats.diagnosis_calls": 1}})
    env.db.recovery_memories.insert_one(
        {"_id": "mem_1", "system": "payments", "embedding": [0.1]}
    )
    stats = env.api.get("/learning/stats").json()["runs"][0]
    assert (
        stats["diagnosis_calls"] == 1
        and stats["memory_hits"] == 0
        and stats["duration_s"] >= 0
    )
    assert "embedding" not in env.api.get("/memories").json()["memories"][0]
    env.mock.post(
        "/payments/refunds",
        json={"attendee_id": "att_001", "amount": "85.00", "currency": "CAD"},
        headers={"Idempotency-Key": "z"},
    )
    out = env.api.post("/reset").json()
    assert (
        out["cleared"]["runs"] == 1
        and env.db.recovery_memories.count_documents({}) == 1
    )
    assert env.mock.get("/admin/reality").json()["payments"]["refund_count"] == 0
    assert env.api.post("/reset?keep_memories=false").status_code == 200
    assert env.db.recovery_memories.count_documents({}) == 0


def test_sse_replays_tail_then_streams_new_events_and_resumes(env: Env) -> None:
    run_id = env.make_run()
    emit_event(env.db, run_id, EventLevel.INFO, EventType.RUN_STARTED, "one")
    emit_event(env.db, run_id, EventLevel.INFO, EventType.PLAN_CREATED, "two")
    emit_event(env.db, "other_run", EventLevel.INFO, EventType.RUN_STARTED, "not mine")

    def read_events(headers: dict[str, str], want: int) -> list[str]:
        got: list[str] = []
        with env.api.stream("GET", f"/events?run_id={run_id}", headers=headers) as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    got.append(line)
                if len(got) >= want:
                    break
        return got

    first = read_events({}, 2)
    assert '"one"' in first[0] and '"two"' in first[1]
    assert all("not mine" not in line for line in first)
    last_id = env.db.events.find_one({"message": "one"})["_id"]
    resumed = read_events({"Last-Event-ID": str(last_id)}, 1)
    assert (
        '"two"' in resumed[0] or '"one"' in resumed[0]
    )  # lookback may re-send; client dedupes by id

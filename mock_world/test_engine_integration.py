"""The real engine (phoenix/worker.py) against the real mock world, in-process.

mongomock has no transactions, so run_in_transaction is patched like in
tests/conftest.py. Atomicity (invariant 6) still needs a real Atlas replica set.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import mongomock
import pytest
from fastapi.testclient import TestClient

import phoenix.db as db_module
from mock_world.app import create_app
from phoenix.db import now_iso
from phoenix.intents import idempotency_key
from phoenix.models import TaskKind
from phoenix.state import mark_sent, record_intent
from phoenix.worker import build_plan, build_request, run_worker_for, send_request

RUN = "run_it_001"
DROP_SLEEP_S = 0.2


class TimeoutClient(TestClient):
    """TestClient never times out. A dropped response is the mock holding the
    reply for DROP_SLEEP_S, so treat any slower POST as the client giving up."""

    def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        start = time.monotonic()
        resp = super().post(*args, **kwargs)
        if time.monotonic() - start >= DROP_SLEEP_S * 0.9:
            raise httpx.ReadTimeout("simulated 3 s client timeout")
        return resp


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db_module, "run_in_transaction", lambda _db, fn: fn(None))
    monkeypatch.setenv("PHOENIX_TASK_DELAY_MS", "0")
    monkeypatch.delenv("PHOENIX_CRASH_AT", raising=False)
    mock = TimeoutClient(create_app(tmp_path / "m.json", drop_sleep_s=DROP_SLEEP_S))
    db = mongomock.MongoClient()["t"]
    ts = now_iso()
    db.runs.insert_one(
        {
            "_id": RUN, "instruction": "cancel", "event_id": "evt_concert_001",
            "status": "PLANNING", "plan_fingerprint": "x", "assumptions": {"booking_id": "book_001"},
            "stats": {}, "api_versions_seen": {}, "created_at": ts, "updated_at": ts,
        }
    )  # fmt: skip
    return mock, db


def reality(mock: TestClient) -> dict[str, Any]:
    return mock.get("/admin/reality").json()


def assert_clean_finish(mock: TestClient, db: Any) -> None:
    r = reality(mock)
    assert r["payments"] == {
        "refund_count": 200,
        "total_minor": 1_700_000,
        "duplicate_refunds": 0,
    }
    assert r["ticketing"]["cancelled"] == 200 and r["notifications"]["sent"] == 200
    assert r["venue"]["status"] == "cancelled"
    assert db.tasks.count_documents({"state": "VERIFIED"}) == 601
    assert db.runs.find_one({"_id": RUN})["status"] == "COMPLETED"


def test_clean_run_matches_the_mock_end_to_end(world) -> None:
    mock, db = world
    run_worker_for(db, mock, RUN)
    assert_clean_finish(mock, db)
    assert db.tasks.count_documents({}) == 601


def test_chaos_drops_and_failures_never_double_refund_or_double_notify(world) -> None:
    mock, db = world
    for system, n in (("payments", 3), ("notifications", 2), ("ticketing", 2)):
        mock.post(
            "/admin/chaos/drop_response_after_commit",
            json={"system": system, "count": n},
        )
    mock.post(
        "/admin/chaos/fail_before_commit", json={"system": "payments", "count": 2}
    )
    mock.post("/admin/chaos/drop_response_after_commit", json={"system": "venue"})
    run_worker_for(db, mock, RUN)
    assert_clean_finish(mock, db)
    assert (
        db.events.count_documents({"type": "TASK_UNKNOWN"}) >= 10
    )  # drops + 500s all landed in UNKNOWN
    assert db.events.count_documents({"type": "NOT_APPLIED_RETRY"}) >= 2


def test_resume_after_crash_between_send_and_result_does_not_resend(world) -> None:
    mock, db = world
    build_plan(db, mock, RUN, "evt_concert_001", "book_001")
    db.runs.update_one({"_id": RUN}, {"$set": {"status": "RUNNING"}})
    # Reproduce a kill -9 right after the requests went out: intent written, state
    # SENT, the mock applied them, and the agent never recorded the responses.
    for task_id in (f"{RUN}:CANCEL_TICKET:att_002", f"{RUN}:REFUND:att_002"):
        task = db.tasks.find_one({"_id": task_id})
        request = build_request(task)
        task = mark_sent(db, record_intent(db, task, request, 1)["_id"])
        send_request(mock, task, request)
    assert mock.get("/admin/reality").json()["payments"]["refund_count"] == 1
    run_worker_for(db, mock, RUN)  # status RUNNING -> recovery path
    r = reality(mock)
    assert r["payments"]["duplicate_refunds"] == 0
    key = idempotency_key("evt_concert_001", TaskKind.REFUND, "att_002")
    found = mock.get("/payments/refunds", params={"idempotency_key": key}).json()[
        "refunds"
    ]
    assert len(found) == 1  # recovered by external read, not resent


def _start_run(db: Any, run_id: str) -> None:
    ts = now_iso()
    db.runs.insert_one(
        {
            "_id": run_id, "instruction": "cancel", "event_id": "evt_concert_001",
            "status": "PLANNING", "plan_fingerprint": "x", "assumptions": {"booking_id": "book_001"},
            "stats": {}, "api_versions_seen": {"payments": 1}, "created_at": ts, "updated_at": ts,
        }
    )  # fmt: skip


def test_payments_api_change_is_healed_by_the_canary_then_reused_in_run_two(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole story with the real worker: v2 switch mid-flight, a deliberately wrong
    first fix caught by the dry run (so no 85-cent refund), then memory reuse in run 2."""
    mock, db = world
    for name in ("GROQ_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_DIM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "none")  # offline: the heuristic proposes
    monkeypatch.setenv("PHOENIX_DEMO_WRONG_FIRST", "1")
    mock.post("/admin/chaos/api_version", json={"system": "payments", "version": 2})

    run_worker_for(db, mock, RUN)  # run 1 (the fixture already created RUN)
    r = reality(mock)
    assert r["payments"] == {
        "refund_count": 200,
        "total_minor": 1_700_000,
        "duplicate_refunds": 0,
    }
    assert r["payments_api_version"] == 2
    assert db.tasks.count_documents({"state": "VERIFIED"}) == 601
    assert db.runs.find_one({"_id": RUN})["status"] == "COMPLETED"
    types = [e["type"] for e in db.events.find({"run_id": RUN})]
    assert types.count("DRIFT_DETECTED") == 1  # only the first refund discovers it
    assert types.count("CANARY_FAILED") == 1 and types.count("CANARY_PASSED") == 1
    assert db.recovery_memories.count_documents({"polarity": "negative"}) == 1
    assert db.recovery_memories.count_documents({"polarity": "positive"}) == 1
    refund = db.tasks.find_one({"_id": f"{RUN}:REFUND:att_200"})
    assert (
        refund["adaptation_spec_id"]
        and refund["verified_effect"]["amount_minor"] == 8500
    )

    # Run 2: fresh world and a fresh run, but the memories survive.
    mock.post("/admin/reset", json={"attendees": 200})
    mock.post("/admin/chaos/api_version", json={"system": "payments", "version": 2})
    _start_run(db, "run_it_002")
    run_worker_for(db, mock, "run_it_002")
    assert reality(mock)["payments"] == {
        "refund_count": 200,
        "total_minor": 1_700_000,
        "duplicate_refunds": 0,
    }
    types2 = [e["type"] for e in db.events.find({"run_id": "run_it_002"})]
    assert (
        "MEMORY_RETRIEVED" in types2 and "CANARY_PASSED" in types2
    )  # recalled AND re-tested
    assert "CANARY_FAILED" not in types2
    stats = db.runs.find_one({"_id": "run_it_002"})["stats"]
    assert stats.get("memory_hits") == 1 and stats.get("diagnosis_calls", 0) == 0
    assert db.recovery_memories.find_one({"polarity": "positive"})["success_count"] == 2

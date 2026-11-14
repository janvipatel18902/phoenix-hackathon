"""Tests for spec validation/apply and the canary flow. Run: pytest -q phoenix/drift/"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import mongomock
import pytest
from fastapi.testclient import TestClient

from mock_world.app import create_app
from phoenix.drift import diagnose, memory
from phoenix.drift.canary import adapt_request, resolve_drift
from phoenix.drift.spec import (
    SpecContext,
    SpecError,
    apply_spec,
    spec_id,
    spec_to_dict,
    validate_spec,
)

V1_BODY = {"attendee_id": "att_088", "amount": "85.00", "currency": "CAD"}
DETAILS = [
    {"field": "amount", "message": "unknown field"},
    {"field": "amount_minor", "message": "required"},
    {"field": "reason_code", "message": "required"},
]
TASK_FIELDS = {"attendee_id", "amount_minor", "currency"}
GOOD_SPEC = {
    "spec_version": 1,
    "system": "payments",
    "operation": "create_refund",
    "ops": [
        {"op": "rename_field", "from": "amount", "to": "amount_minor"},
        {
            "op": "scale_value",
            "field": "amount_minor",
            "factor": 100,
            "from_type": "decimal_string",
            "to_type": "integer",
            "rounding": "half_up",
        },
        {
            "op": "add_field",
            "field": "reason_code",
            "value": {"source": "constant", "value": "EVENT_CANCELLED"},
        },
    ],
}


def ctx() -> SpecContext:
    return SpecContext(dict(V1_BODY), DETAILS, set(TASK_FIELDS))


def with_ops(*ops: dict[str, Any]) -> dict[str, Any]:
    return {**GOOD_SPEC, "ops": list(ops)}


# --- spec ---


def test_good_spec_applies_to_contract_v2_body() -> None:
    spec = validate_spec(GOOD_SPEC, ctx())
    assert apply_spec(spec, V1_BODY, {}) == {
        "attendee_id": "att_088",
        "amount_minor": 8500,
        "currency": "CAD",
        "reason_code": "EVENT_CANCELLED",
    }
    assert V1_BODY["amount"] == "85.00"  # apply is pure


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "eval", "code": "1"},
        {"op": "rename_field", "from": "amount", "to": "not_in_details"},
        {"op": "rename_field", "from": "ghost", "to": "amount_minor"},
        {
            "op": "scale_value",
            "field": "amount",
            "factor": 0,
            "from_type": "decimal_string",
            "to_type": "integer",
        },
        {
            "op": "scale_value",
            "field": "amount",
            "factor": 1e9,
            "from_type": "decimal_string",
            "to_type": "integer",
        },
        {
            "op": "scale_value",
            "field": "amount",
            "factor": "abc",
            "from_type": "decimal_string",
            "to_type": "integer",
        },
        {
            "op": "scale_value",
            "field": "amount",
            "factor": True,
            "from_type": "decimal_string",
            "to_type": "integer",
        },
        {
            "op": "scale_value",
            "field": "amount",
            "factor": "NaN",
            "from_type": "decimal_string",
            "to_type": "integer",
        },
        {
            "op": "add_field",
            "field": "secret",
            "value": {"source": "constant", "value": "x"},
        },
        {
            "op": "add_field",
            "field": "reason_code",
            "value": {"source": "shell", "value": "ls"},
        },
        {
            "op": "add_field",
            "field": "reason_code",
            "value": {"source": "task_field", "value": "nope"},
        },
        {"op": "remove_field", "field": "currency"},
    ],
)
def test_validator_rejects_unsafe_or_unknown_ops(bad: dict[str, Any]) -> None:
    with pytest.raises(SpecError):
        validate_spec(with_ops(bad), ctx())


def test_validator_rejects_extra_keys_and_too_many_ops() -> None:
    with pytest.raises(SpecError):
        validate_spec({**GOOD_SPEC, "run": "rm -rf"}, ctx())
    many = [{"op": "remove_field", "field": "amount"}] * 11
    with pytest.raises(SpecError):
        validate_spec(with_ops(*many), ctx())


def test_scale_rounds_half_up_and_spec_id_is_stable() -> None:
    spec = validate_spec(
        with_ops(
            {"op": "rename_field", "from": "amount", "to": "amount_minor"},
            {
                "op": "scale_value",
                "field": "amount_minor",
                "factor": 100,
                "from_type": "decimal_string",
                "to_type": "integer",
            },
        ),
        ctx(),
    )
    assert apply_spec(spec, {**V1_BODY, "amount": "0.125"}, {})["amount_minor"] == 13
    assert spec_id(spec) == spec_id(validate_spec(spec_to_dict(spec), ctx()))


# --- diagnose ---


def test_description_matches_contract_example() -> None:
    sig = diagnose.build_signature("payments", 422, {"details": DETAILS})
    assert diagnose.describe_signature("payments", sig) == (
        "payments create_refund returned 422: unknown field amount; "
        "missing fields amount_minor, reason_code"
    )


def test_heuristic_builds_the_correct_spec_and_wrong_first_scales_by_one(
    monkeypatch,
) -> None:
    sig = diagnose.build_signature("payments", 422, {"details": DETAILS})
    good = validate_spec(diagnose.heuristic_spec("payments", sig, ctx()), ctx())
    assert apply_spec(good, V1_BODY, {})["amount_minor"] == 8500
    wrong = validate_spec(
        diagnose.heuristic_spec("payments", sig, ctx(), wrong_first=True), ctx()
    )
    assert apply_spec(wrong, V1_BODY, {})["amount_minor"] == 85


def test_heuristic_refuses_unknown_required_field() -> None:
    details = [*DETAILS, {"field": "mystery", "message": "required"}]
    sig = diagnose.build_signature("payments", 422, {"details": details})
    with pytest.raises(SpecError):
        diagnose.heuristic_spec(
            "payments", sig, SpecContext(dict(V1_BODY), details, set(TASK_FIELDS))
        )


# --- canary flow against the real mock world ---


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.mock = TestClient(create_app(tmp_path / "s.json", drop_sleep_s=0))
        self.mock.post(
            "/admin/chaos/api_version", json={"system": "payments", "version": 2}
        )
        self.db = mongomock.MongoClient()["t"]
        self.events: list[tuple[str, str]] = []
        self.task = {
            "_id": "run_1:REFUND:att_088",
            "run_id": "run_1",
            "system": "payments",
            "ref": "att_088",
            "expected_effect": {
                "attendee_id": "att_088",
                "amount_minor": 8500,
                "currency": "CAD",
            },
        }
        self.new_run("run_1")

    def new_run(self, run_id: str) -> None:
        self.db.runs.insert_one(
            {"_id": run_id, "stats": {}, "api_versions_seen": {"payments": 1}}
        )
        self.task = {**self.task, "run_id": run_id, "_id": f"{run_id}:REFUND:att_088"}

    def emit(
        self, db: Any, run_id: str, level: Any, type_: Any, message: str, **kw: Any
    ) -> None:
        self.events.append((type_.value, message))

    def drift(self) -> Any:
        resp = self.mock.post(
            "/payments/refunds", json=V1_BODY, headers={"Idempotency-Key": "k"}
        )
        return resolve_drift(
            self.db,
            self.mock,
            self.task,
            dict(V1_BODY),
            resp.status_code,
            resp.json(),
            emit=self.emit,
        )

    def types(self) -> list[str]:
        return [t for t, _ in self.events]

    def refunds(self) -> int:
        return self.mock.get("/admin/reality").json()["payments"]["refund_count"]


@pytest.fixture
def h(tmp_path: Path, monkeypatch) -> Harness:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("PHOENIX_DEMO_WRONG_FIRST", raising=False)
    return Harness(tmp_path)


def test_not_drift_is_ignored(h: Harness) -> None:
    out = resolve_drift(h.db, h.mock, h.task, dict(V1_BODY), 500, {}, emit=h.emit)
    assert out.status == "NOT_DRIFT" and h.events == []


def test_correct_proposal_passes_canary_and_activates_without_sending(
    h: Harness,
) -> None:
    out = h.drift()
    assert out.status == "ADAPTED"
    assert h.types() == [
        "DRIFT_DETECTED", "ADAPTATION_PROPOSED", "CANARY_STARTED", "CANARY_PASSED", "MEMORY_SAVED",
    ]  # fmt: skip
    assert h.refunds() == 0  # the canary is a dry run: no money moved
    request, sid = adapt_request(h.db, h.task, dict(V1_BODY))
    assert sid == out.spec_id and request["amount_minor"] == 8500
    assert h.db.recovery_memories.count_documents({"polarity": "positive"}) == 1
    assert h.db.effects.find_one({"source": "canary"})["matches_expected"] is True
    assert h.db.runs.find_one({"_id": "run_1"})["stats"]["diagnosis_calls"] == 1
    assert (
        h.mock.post(
            "/payments/refunds", json=request, headers={"Idempotency-Key": "k"}
        ).status_code
        == 201
    )


def test_wrong_first_proposal_is_caught_then_correct_one_wins(
    h: Harness, monkeypatch
) -> None:
    monkeypatch.setenv("PHOENIX_DEMO_WRONG_FIRST", "1")
    out = h.drift()
    assert out.status == "ADAPTED"
    assert (
        h.types().count("CANARY_FAILED") == 1 and h.types().count("CANARY_PASSED") == 1
    )
    assert h.refunds() == 0  # the 85-cent refund never happened
    assert h.db.recovery_memories.count_documents({"polarity": "negative"}) == 1
    failed = h.db.effects.find_one({"matches_expected": False})
    assert failed["observation"]["amount_minor"] == 85
    request, _ = adapt_request(h.db, h.task, dict(V1_BODY))
    assert request["amount_minor"] == 8500


def test_run_two_reuses_the_memory_but_still_runs_the_canary(
    h: Harness, monkeypatch
) -> None:
    monkeypatch.setenv("PHOENIX_DEMO_WRONG_FIRST", "1")
    h.drift()
    h.events.clear()
    h.new_run("run_2")
    out = h.drift()
    assert out.status == "ADAPTED"
    types = h.types()
    assert "MEMORY_RETRIEVED" in types and "CANARY_PASSED" in types
    assert "CANARY_FAILED" not in types  # the known-bad spec is not retried
    stats = h.db.runs.find_one({"_id": "run_2"})["stats"]
    assert stats.get("memory_hits") == 1 and stats.get("diagnosis_calls", 0) == 0
    mem = h.db.recovery_memories.find_one({"polarity": "positive"})
    assert mem["success_count"] == 2  # reinforced, not duplicated
    assert h.db.recovery_memories.count_documents({"polarity": "positive"}) == 1


def test_poisoned_memory_is_not_trusted_without_the_canary(h: Harness) -> None:
    sig = diagnose.build_signature("payments", 422, {"details": DETAILS})
    wrong = diagnose.heuristic_spec("payments", sig, ctx(), wrong_first=True)
    memory.save_memory(
        h.db, run_id="old", system="payments", signature=sig,
        description=diagnose.describe_signature("payments", sig), spec=wrong,
        canary_result={}, polarity="positive",
    )  # fmt: skip
    out = h.drift()
    assert out.status == "ADAPTED"
    assert "MEMORY_RETRIEVED" in h.types() and "CANARY_FAILED" in h.types()
    assert h.refunds() == 0
    assert adapt_request(h.db, h.task, dict(V1_BODY))[0]["amount_minor"] == 8500


def test_unreachable_payments_is_needs_human_not_a_failed_canary(h: Harness) -> None:
    class Down:
        def post(self, *a: Any, **k: Any) -> Any:
            import httpx

            raise httpx.ConnectError("down")

    resp = h.mock.post(
        "/payments/refunds", json=V1_BODY, headers={"Idempotency-Key": "k"}
    )
    out = resolve_drift(
        h.db, Down(), h.task, dict(V1_BODY), 422, resp.json(), emit=h.emit
    )  # type: ignore[arg-type]
    assert out.status == "NEEDS_HUMAN"
    assert h.db.recovery_memories.count_documents({}) == 0
    assert adapt_request(h.db, h.task, dict(V1_BODY))[1] is None


def test_embedding_is_deterministic_and_retrieval_scans_without_an_index(
    h: Harness,
) -> None:
    a = memory.embed("payments create_refund returned 422")
    assert a == memory.embed("payments create_refund returned 422") and len(a) == 256
    h.drift()
    found = memory.retrieve(
        h.db,
        "payments",
        "payments create_refund returned 422: unknown field amount; missing fields amount_minor, reason_code",
        "positive",
    )
    assert found.via == "scan" and len(found.hits) == 1
    unrelated = memory.retrieve(
        h.db, "payments", "venue booking cancelled twice", "positive"
    )
    assert unrelated.hits == []

"""Checks for the mock world. Run: pytest -q mock_world/"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mock_world.app import create_app

KEY = "phx:evt_concert_001:REFUND:att_088"
V1_BODY = {"attendee_id": "att_088", "amount": "85.00", "currency": "CAD"}
V2_BODY = {
    "attendee_id": "att_088",
    "amount_minor": 8500,
    "currency": "CAD",
    "reason_code": "EVENT_CANCELLED",
}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "state.json", drop_sleep_s=0.2))


def reality(c: TestClient) -> dict:
    return c.get("/admin/reality").json()


def refund(c: TestClient, body: dict = V1_BODY, key: str = KEY) -> httpx.Response:
    return c.post("/payments/refunds", json=body, headers={"Idempotency-Key": key})


def test_seed_is_200_attendees_at_85_cad(client: TestClient) -> None:
    data = client.get("/ticketing/events/evt_concert_001/attendees").json()
    assert data["version"] == 12 and len(data["attendees"]) == 200
    assert {a["amount_paid"] for a in data["attendees"]} == {"85.00"}
    assert data["attendees"][87]["attendee_id"] == "att_088"


def test_normal_refund_and_lookup_by_key(client: TestClient) -> None:
    r = refund(client)
    assert r.status_code == 201
    assert r.json()["amount"] == "85.00" and r.json()["status"] == "succeeded"
    found = client.get("/payments/refunds", params={"idempotency_key": KEY}).json()[
        "refunds"
    ]
    assert len(found) == 1 and found[0]["amount"] == "85.00"
    assert reality(client)["payments"] == {
        "refund_count": 1,
        "total_minor": 8500,
        "duplicate_refunds": 0,
    }


def test_replayed_key_does_not_double_refund(client: TestClient) -> None:
    first = refund(client).json()
    again = refund(client)
    assert again.status_code == 200
    assert again.headers["Idempotent-Replay"] == "true"
    assert again.json() == first
    assert reality(client)["payments"]["refund_count"] == 1


def test_different_key_same_attendee_counts_as_duplicate(client: TestClient) -> None:
    refund(client)
    refund(client, key="other")
    assert reality(client)["payments"]["duplicate_refunds"] == 1


def test_drop_response_after_commit_applies_but_client_times_out(
    tmp_path: Path,
) -> None:
    c = TestClient(create_app(tmp_path / "s.json", drop_sleep_s=0.5))
    c.post(
        "/admin/chaos/drop_response_after_commit",
        json={"system": "payments", "count": 1},
    )
    start = time.monotonic()
    r = refund(c)
    assert (
        time.monotonic() - start >= 0.5
    )  # handler held the response past the "timeout"
    assert r.status_code == 201
    # the commit stands, and the lookup by key proves it
    assert (
        len(
            c.get("/payments/refunds", params={"idempotency_key": KEY}).json()[
                "refunds"
            ]
        )
        == 1
    )
    # only one drop was armed: the next write is fast
    assert c.get("/admin/chaos").json()["drop_response_after_commit"] == []


def test_fail_before_commit_does_not_apply(client: TestClient) -> None:
    client.post(
        "/admin/chaos/fail_before_commit", json={"system": "payments", "count": 1}
    )
    assert refund(client).status_code == 500
    assert reality(client)["payments"]["refund_count"] == 0
    assert refund(client).status_code == 201  # same key works once the fault is spent


def test_v2_switch_rejects_v1_body_with_contract_422(client: TestClient) -> None:
    client.post("/admin/chaos/api_version", json={"system": "payments", "version": 2})
    r = refund(client)
    assert r.status_code == 422
    assert r.json() == {
        "error": "validation_error",
        "details": [
            {"field": "amount", "message": "unknown field"},
            {"field": "amount_minor", "message": "required"},
            {"field": "reason_code", "message": "required"},
        ],
    }
    assert reality(client)["payments"]["refund_count"] == 0
    ok = refund(client, V2_BODY)
    assert ok.status_code == 201 and ok.json()["amount_minor"] == 8500
    assert "amount" not in ok.json()
    found = client.get("/payments/refunds", params={"idempotency_key": KEY}).json()[
        "refunds"
    ]
    assert found[0]["amount_minor"] == 8500


def test_dry_run_commits_nothing_and_normalizes(client: TestClient) -> None:
    r = client.post("/payments/refunds?dry_run=true", json=V1_BODY)
    assert r.status_code == 200
    assert r.json() == {
        "dry_run": True,
        "would_store": {
            "attendee_id": "att_088",
            "amount_minor": 8500,
            "currency": "CAD",
        },
    }
    assert reality(client)["payments"]["refund_count"] == 0
    assert client.get("/payments/refunds").json() == {"refunds": []}
    # a dry run does not claim the idempotency key either
    assert refund(client).status_code == 201


def test_dry_run_under_v2_catches_wrong_spec_and_invalid_body(
    client: TestClient,
) -> None:
    client.post("/admin/chaos/api_version", json={"system": "payments", "version": 2})
    wrong = {
        **V2_BODY,
        "amount_minor": 85,
    }  # the wrong first proposal: cents, not scaled
    r = client.post("/payments/refunds?dry_run=true", json=wrong)
    assert r.json()["would_store"]["amount_minor"] == 85
    bad = client.post("/payments/refunds?dry_run=true", json=V1_BODY)
    assert bad.status_code == 422
    assert bad.json() == refund(client).json()  # same 422 as the real call
    assert reality(client)["payments"]["refund_count"] == 0


def test_chaos_does_not_affect_gets_or_dry_runs(client: TestClient) -> None:
    client.post("/admin/chaos/latency", json={"system": "payments", "ms": 800})
    client.post(
        "/admin/chaos/fail_before_commit", json={"system": "payments", "count": 5}
    )
    client.post("/admin/chaos/drop_response_after_commit", json={"system": "payments"})
    start = time.monotonic()
    assert (
        client.get("/payments/refunds", params={"idempotency_key": KEY}).status_code
        == 200
    )
    assert client.get("/ticketing/attendees/att_001").status_code == 200
    assert (
        client.get("/notifications/emails", params={"reference": "att_001"}).status_code
        == 200
    )
    assert client.get("/venue/bookings/book_001").status_code == 200
    assert (
        client.post("/payments/refunds?dry_run=true", json=V1_BODY).status_code == 200
    )
    assert reality(client)["payments_api_version"] == 1
    assert time.monotonic() - start < 0.5
    # none of that consumed the armed rules
    assert len(client.get("/admin/chaos").json()["fail_before_commit"]) == 1


def test_latency_delays_posts_only(client: TestClient) -> None:
    client.post(
        "/admin/chaos/latency", json={"system": "notifications", "ms": 400, "count": 1}
    )
    body = {"to": "a@b.com", "template": "event_cancelled", "reference": "att_001"}
    start = time.monotonic()
    assert client.post("/notifications/emails", json=body).status_code == 201
    assert time.monotonic() - start >= 0.4
    start = time.monotonic()
    emails = client.get(
        "/notifications/emails", params={"reference": "att_001"}
    ).json()["emails"]
    assert len(emails) == 1 and time.monotonic() - start < 0.2


def test_venue_is_one_shot(client: TestClient) -> None:
    first = client.post("/venue/bookings/book_001/cancel")
    assert first.status_code == 200 and first.json()["fee_minor"] == 25000
    second = client.post("/venue/bookings/book_001/cancel")
    assert second.status_code == 409 and second.json() == {"error": "already_cancelled"}
    assert client.get("/venue/bookings/book_001").json()["status"] == "cancelled"


def test_ticket_cancel_and_nth_drop(client: TestClient) -> None:
    client.post(
        "/admin/chaos/drop_response_after_commit",
        json={"system": "ticketing", "nth": 2},
    )
    for i in (1, 2, 3):
        t0 = time.monotonic()
        assert client.post(f"/ticketing/tickets/tkt_00{i}/cancel").status_code == 200
        assert (time.monotonic() - t0 >= 0.2) == (i == 2)
    assert reality(client)["ticketing"]["cancelled"] == 3


def test_mutate_record_bumps_versions_and_clear_resets_v1(client: TestClient) -> None:
    client.post(
        "/admin/chaos/mutate_record",
        json={"attendee_id": "att_120", "fields": {"email": "n@e.com"}},
    )
    att = client.get("/ticketing/attendees/att_120").json()
    assert att["email"] == "n@e.com" and att["record_version"] == 4
    assert (
        client.get("/ticketing/events/evt_concert_001/attendees").json()["version"]
        == 13
    )
    client.post("/admin/chaos/api_version", json={"system": "payments", "version": 2})
    client.post("/admin/chaos/clear")
    assert reality(client)["payments_api_version"] == 1


def test_state_survives_restart_and_reset_reseeds(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    c1 = TestClient(create_app(path, drop_sleep_s=0))
    refund(c1)
    c2 = TestClient(create_app(path, drop_sleep_s=0))  # "process restart"
    assert reality(c2)["payments"]["refund_count"] == 1
    c2.post("/admin/reset", json={"attendees": 5})
    assert reality(c2)["payments"]["refund_count"] == 0
    assert (
        len(c2.get("/ticketing/events/evt_concert_001/attendees").json()["attendees"])
        == 5
    )

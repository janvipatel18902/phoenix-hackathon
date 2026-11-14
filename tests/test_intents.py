from phoenix.intents import idempotency_key, request_hash
from phoenix.models import TaskKind


def test_idempotency_key_format():
    assert (
        idempotency_key("evt_concert_001", TaskKind.REFUND, "att_088")
        == "phx:evt_concert_001:REFUND:att_088"
    )


def test_idempotency_key_is_deterministic_not_random():
    # Same event/kind/ref always yields the same key, independent of run id --
    # this is what lets reconcile recompute keys after total state loss.
    a = idempotency_key("evt_concert_001", TaskKind.CANCEL_TICKET, "att_001")
    b = idempotency_key("evt_concert_001", TaskKind.CANCEL_TICKET, "att_001")
    assert a == b


def test_idempotency_key_varies_by_ref_and_kind():
    a = idempotency_key("evt_concert_001", TaskKind.REFUND, "att_001")
    b = idempotency_key("evt_concert_001", TaskKind.REFUND, "att_002")
    c = idempotency_key("evt_concert_001", TaskKind.NOTIFY, "att_001")
    assert len({a, b, c}) == 3


def test_request_hash_stable_regardless_of_key_order():
    h1 = request_hash({"a": 1, "b": 2})
    h2 = request_hash({"b": 2, "a": 1})
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_request_hash_differs_for_different_bodies():
    assert request_hash({"amount": "85.00"}) != request_hash({"amount": "85.01"})

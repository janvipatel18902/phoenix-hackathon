from __future__ import annotations

import httpx
import respx

from phoenix.verify import (
    verify_cancel_ticket,
    verify_notify,
    verify_refund,
    verify_release_venue,
)


@respx.mock
def test_verify_cancel_ticket_applied_and_matches():
    respx.get("https://mock/ticketing/attendees/att_001").mock(
        return_value=httpx.Response(200, json={"ticket_id": "tkt_001", "ticket_status": "cancelled"})
    )
    client = httpx.Client(base_url="https://mock")
    result = verify_cancel_ticket(client, "att_001", {"ticket_id": "tkt_001", "ticket_status": "cancelled"})
    assert result.applied is True
    assert result.matches_expected is True


@respx.mock
def test_verify_cancel_ticket_not_applied():
    respx.get("https://mock/ticketing/attendees/att_001").mock(
        return_value=httpx.Response(200, json={"ticket_id": "tkt_001", "ticket_status": "active"})
    )
    client = httpx.Client(base_url="https://mock")
    result = verify_cancel_ticket(client, "att_001", {"ticket_id": "tkt_001", "ticket_status": "cancelled"})
    assert result.applied is False


@respx.mock
def test_verify_cancel_ticket_network_error_cannot_determine():
    respx.get("https://mock/ticketing/attendees/att_001").mock(side_effect=httpx.ConnectError("boom"))
    client = httpx.Client(base_url="https://mock")
    result = verify_cancel_ticket(client, "att_001", {"ticket_id": "tkt_001", "ticket_status": "cancelled"})
    assert result.applied is None
    assert result.matches_expected is None


@respx.mock
def test_verify_refund_not_found_is_not_applied():
    respx.get("https://mock/payments/refunds").mock(return_value=httpx.Response(200, json={"refunds": []}))
    client = httpx.Client(base_url="https://mock")
    result = verify_refund(client, "phx:evt:REFUND:att_001", {"attendee_id": "att_001", "amount_minor": 8500, "currency": "CAD"})
    assert result.applied is False


@respx.mock
def test_verify_refund_v1_dollar_string_normalized_to_minor_units():
    respx.get("https://mock/payments/refunds").mock(
        return_value=httpx.Response(
            200,
            json={"refunds": [{"attendee_id": "att_001", "amount": "85.00", "currency": "CAD"}]},
        )
    )
    client = httpx.Client(base_url="https://mock")
    result = verify_refund(
        client, "phx:evt:REFUND:att_001", {"attendee_id": "att_001", "amount_minor": 8500, "currency": "CAD"}
    )
    assert result.applied is True
    assert result.observation["amount_minor"] == 8500
    assert result.matches_expected is True


@respx.mock
def test_verify_refund_v2_amount_minor_passthrough():
    respx.get("https://mock/payments/refunds").mock(
        return_value=httpx.Response(
            200,
            json={"refunds": [{"attendee_id": "att_001", "amount_minor": 8500, "currency": "CAD"}]},
        )
    )
    client = httpx.Client(base_url="https://mock")
    result = verify_refund(
        client, "phx:evt:REFUND:att_001", {"attendee_id": "att_001", "amount_minor": 8500, "currency": "CAD"}
    )
    assert result.applied is True
    assert result.matches_expected is True


@respx.mock
def test_verify_refund_amount_mismatch_never_auto_corrected():
    respx.get("https://mock/payments/refunds").mock(
        return_value=httpx.Response(
            200,
            json={"refunds": [{"attendee_id": "att_001", "amount_minor": 1, "currency": "CAD"}]},
        )
    )
    client = httpx.Client(base_url="https://mock")
    result = verify_refund(
        client, "phx:evt:REFUND:att_001", {"attendee_id": "att_001", "amount_minor": 8500, "currency": "CAD"}
    )
    assert result.applied is True
    assert result.matches_expected is False


@respx.mock
def test_verify_notify_counts_sent():
    respx.get("https://mock/notifications/emails").mock(
        return_value=httpx.Response(
            200, json={"emails": [{"message_id": "m1", "to": "a@b.com", "template": "event_cancelled", "reference": "att_001", "sent_at": "x"}]}
        )
    )
    client = httpx.Client(base_url="https://mock")
    result = verify_notify(client, "att_001", {"reference": "att_001", "template": "event_cancelled", "sent_count": 1})
    assert result.applied is True
    assert result.matches_expected is True


@respx.mock
def test_verify_release_venue_cancelled():
    respx.get("https://mock/venue/bookings/book_001").mock(
        return_value=httpx.Response(200, json={"booking_id": "book_001", "status": "cancelled"})
    )
    client = httpx.Client(base_url="https://mock")
    result = verify_release_venue(client, "book_001", {"booking_id": "book_001", "status": "cancelled"})
    assert result.applied is True
    assert result.matches_expected is True


@respx.mock
def test_verify_release_venue_still_active():
    respx.get("https://mock/venue/bookings/book_001").mock(
        return_value=httpx.Response(200, json={"booking_id": "book_001", "status": "active"})
    )
    client = httpx.Client(base_url="https://mock")
    result = verify_release_venue(client, "book_001", {"booking_id": "book_001", "status": "cancelled"})
    assert result.applied is False


@respx.mock
def test_verify_malformed_response_cannot_determine():
    respx.get("https://mock/venue/bookings/book_001").mock(return_value=httpx.Response(200, json={"oops": True}))
    client = httpx.Client(base_url="https://mock")
    result = verify_release_venue(client, "book_001", {"booking_id": "book_001", "status": "cancelled"})
    assert result.applied is None
    assert result.matches_expected is None

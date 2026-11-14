"""Mock external world (contracts/CONTRACTS.md section 5): ticketing, payments,
notifications and venue, plus chaos and admin endpoints, in one process.

State lives in memory behind one lock and is persisted to a JSON file (NOT
MongoDB) after every mutation, so the mock keeps "reality" even when the agent
and its database are wiped. Run: `uvicorn mock_world.app:app --port 8001`.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

System = Literal["ticketing", "payments", "notifications", "venue"]

REASON_CODES = ["EVENT_CANCELLED", "CUSTOMER_REQUEST", "DUPLICATE", "FRAUD"]
REFUND_FIELDS = {
    1: ["attendee_id", "amount", "currency"],
    2: ["attendee_id", "amount_minor", "currency", "reason_code"],
}
DECIMAL_RE = re.compile(r"^\d+\.\d{2}$")
BOOKING_ID = "book_001"
VENUE_FEE_MINOR = 25000
SEED_AMOUNT = "85.00"
SEED_RECORD_VERSION = 3
SEED_LIST_VERSION = 12
MUTABLE_FIELDS = {"email", "name", "amount_paid"}
DEFAULT_STATE_PATH = Path(__file__).with_name("state.json")


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def minor_to_decimal_string(minor: int) -> str:
    return f"{minor // 100}.{minor % 100:02d}"


def decimal_string_to_minor(value: str) -> int:
    return int((Decimal(value) * 100).to_integral_value())


def seed_data(event_id: str, attendees: int, seed: int) -> dict[str, Any]:
    people: dict[str, Any] = {}
    for i in range(1, attendees + 1):
        att_id = f"att_{i:03d}"
        people[att_id] = {
            "attendee_id": att_id,
            "name": f"Attendee {i:03d}",
            "email": f"{att_id}@example.com",
            "ticket_id": f"tkt_{i:03d}",
            "ticket_status": "active",
            "amount_paid": SEED_AMOUNT,
            "record_version": SEED_RECORD_VERSION,
        }
    return {
        "event_id": event_id,
        "seed": seed,
        "list_version": SEED_LIST_VERSION,
        "seq": 0,
        "attendees": people,
        "refunds": [],
        "emails": [],
        "venue": {"booking_id": BOOKING_ID, "status": "active"},
        "chaos": empty_chaos(),
    }


def empty_chaos() -> dict[str, Any]:
    return {
        "api_version": {"payments": 1},
        "drop_response_after_commit": [],
        "fail_before_commit": [],
        "latency": [],
    }


@dataclass
class Result:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    committed: bool = False

    def response(self) -> JSONResponse:
        return JSONResponse(self.body, status_code=self.status, headers=self.headers)


def validation_error(details: list[dict[str, str]]) -> Result:
    return Result(422, {"error": "validation_error", "details": details})


def field_errors(
    body: Any, fields: list[str], optional: list[str] | None = None
) -> list[dict[str, str]]:
    """Unknown fields first, then missing required ones (the order drift parses)."""
    if not isinstance(body, dict):
        return [{"field": "body", "message": "must be a JSON object"}]
    allowed = set(fields) | set(optional or [])
    unknown = [
        {"field": k, "message": "unknown field"} for k in body if k not in allowed
    ]
    missing = [{"field": k, "message": "required"} for k in fields if k not in body]
    return unknown + missing


class World:
    """All mock state. Every public method that touches `data` holds `lock`."""

    def __init__(self, path: Path, drop_sleep_s: float) -> None:
        self.path = path
        self.drop_sleep_s = drop_sleep_s
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            return json.loads(self.path.read_text())
        data = seed_data("evt_concert_001", 200, 1)
        self._write(data)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        # Atomic replace so a kill -9 mid-write can never leave a torn file.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.path)

    def save(self) -> None:
        self._write(self.data)

    def next_id(self, prefix: str) -> str:
        self.data["seq"] += 1
        return f"{prefix}_{self.data['seq']:06d}"

    # --- chaos plumbing ---

    def take_rule(self, kind: str, system: str) -> dict[str, Any] | None:
        """Consume and return the first rule of `kind` that fires for `system`.
        Caller holds the lock."""
        rules: list[dict[str, Any]] = self.data["chaos"][kind]
        for rule in list(rules):
            if rule["system"] != system:
                continue
            fired = self._advance(rule)
            if rule.get("exhausted"):
                rules.remove(rule)
            if fired:
                self.save()
                return rule
            self.save()
        return None

    @staticmethod
    def _advance(rule: dict[str, Any]) -> bool:
        nth = rule.get("nth")
        if nth is not None:
            rule["seen"] = rule.get("seen", 0) + 1
            if rule["seen"] == nth:
                rule["exhausted"] = True
                return True
            return False
        count = rule.get("count")
        if count is None:
            return True
        rule["count"] = count - 1
        if rule["count"] <= 0:
            rule["exhausted"] = True
        return True

    def run_write(
        self, system: str, prepare: Callable[[], Result | Callable[[], Result]]
    ) -> Result:
        """Chaos-aware committing write: latency, validate, fail, commit, drop.

        `prepare` runs under the lock and returns either a final Result
        (validation error, 404) or an `apply` callable that commits.
        """
        with self.lock:
            latency = self.take_rule("latency", system)
        if latency is not None:
            time.sleep(latency["ms"] / 1000.0)  # outside the lock: reads stay live

        with self.lock:
            plan = prepare()
            if isinstance(plan, Result):
                return plan
            if self.take_rule("fail_before_commit", system) is not None:
                return Result(500, {"error": "internal_error"})
            result = plan()
            drop = result.committed and self.take_rule(
                "drop_response_after_commit", system
            )
        if drop:
            # The write is committed; the client gives up after its own timeout.
            time.sleep(self.drop_sleep_s)
        return result

    # --- ticketing ---

    def find_by_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        for att in self.data["attendees"].values():
            if att["ticket_id"] == ticket_id:
                return att
        return None

    def cancel_ticket(self, ticket_id: str) -> Result:
        def prepare() -> Result | Callable[[], Result]:
            att = self.find_by_ticket(ticket_id)
            if att is None:
                return Result(404, {"error": "ticket_not_found"})

            def apply() -> Result:
                att["ticket_status"] = "cancelled"  # naturally idempotent
                self.save()
                return Result(
                    200,
                    {"ticket_id": ticket_id, "ticket_status": "cancelled"},
                    committed=True,
                )

            return apply

        return self.run_write("ticketing", prepare)

    # --- payments ---

    def refund_version(self) -> int:
        return int(self.data["chaos"]["api_version"]["payments"])

    def validate_refund(self, body: Any) -> tuple[list[dict[str, str]], dict[str, Any]]:
        """Returns (errors, normalized). Normalized is always minor units."""
        version = self.refund_version()
        fields = REFUND_FIELDS[version]
        errors = field_errors(body, fields)
        if errors:
            return errors, {}
        if body["attendee_id"] not in self.data["attendees"]:
            errors.append({"field": "attendee_id", "message": "unknown attendee"})
        if body["currency"] != "CAD":
            errors.append({"field": "currency", "message": "unsupported currency"})
        minor = 0
        if version == 1:
            amount = body["amount"]
            if not isinstance(amount, str) or not DECIMAL_RE.match(amount):
                errors.append(
                    {
                        "field": "amount",
                        "message": "must be a decimal string with 2 places",
                    }
                )
            else:
                minor = decimal_string_to_minor(amount)
        else:
            minor_in = body["amount_minor"]
            if isinstance(minor_in, bool) or not isinstance(minor_in, int):
                errors.append(
                    {"field": "amount_minor", "message": "must be an integer"}
                )
            else:
                minor = minor_in
            if body["reason_code"] not in REASON_CODES:
                errors.append(
                    {"field": "reason_code", "message": "invalid reason_code"}
                )
        if not errors and minor <= 0:
            errors.append({"field": "amount", "message": "must be positive"})
        if errors:
            return errors, {}
        normalized: dict[str, Any] = {
            "attendee_id": body["attendee_id"],
            "amount_minor": minor,
            "currency": body["currency"],
        }
        if version == 2:
            normalized["reason_code"] = body["reason_code"]
        return [], normalized

    def render_refund(self, refund: dict[str, Any]) -> dict[str, Any]:
        out = {
            "refund_id": refund["refund_id"],
            "idempotency_key": refund["idempotency_key"],
            "attendee_id": refund["attendee_id"],
            "currency": refund["currency"],
            "status": refund["status"],
            "created_at": refund["created_at"],
        }
        if self.refund_version() == 1:
            out["amount"] = minor_to_decimal_string(refund["amount_minor"])
        else:
            out["amount_minor"] = refund["amount_minor"]
            out["reason_code"] = refund["reason_code"]
        return out

    def dry_run_refund(self, body: Any) -> Result:
        with self.lock:
            errors, normalized = self.validate_refund(body)
        if errors:
            return validation_error(errors)
        return Result(200, {"dry_run": True, "would_store": normalized})

    def create_refund(self, body: Any, key: str | None) -> Result:
        def prepare() -> Result | Callable[[], Result]:
            errors, normalized = self.validate_refund(body)
            if errors:
                return validation_error(errors)
            if not key:
                return Result(400, {"error": "idempotency_key_required"})
            existing = next(
                (r for r in self.data["refunds"] if r["idempotency_key"] == key), None
            )
            if existing is not None:
                return lambda: Result(
                    200, self.render_refund(existing), {"Idempotent-Replay": "true"}
                )

            def apply() -> Result:
                refund = {
                    "refund_id": self.next_id("ref"),
                    "idempotency_key": key,
                    "attendee_id": normalized["attendee_id"],
                    "amount_minor": normalized["amount_minor"],
                    "currency": normalized["currency"],
                    "reason_code": normalized.get("reason_code"),
                    "status": "succeeded",
                    "created_at": now_iso(),
                }
                self.data["refunds"].append(refund)
                self.save()
                return Result(201, self.render_refund(refund), committed=True)

            return apply

        return self.run_write("payments", prepare)

    def list_refunds(self, key: str | None, attendee_id: str | None) -> dict[str, Any]:
        with self.lock:
            refunds = [
                r
                for r in self.data["refunds"]
                if (key is None or r["idempotency_key"] == key)
                and (attendee_id is None or r["attendee_id"] == attendee_id)
            ]
            return {"refunds": [self.render_refund(r) for r in refunds]}

    # --- notifications ---

    def send_email(self, body: Any) -> Result:
        def prepare() -> Result | Callable[[], Result]:
            errors = field_errors(body, ["to", "template", "reference"])
            if errors:
                return validation_error(errors)

            def apply() -> Result:
                email = {
                    "message_id": self.next_id("msg"),
                    "to": body["to"],
                    "template": body["template"],
                    "reference": body["reference"],
                    "sent_at": now_iso(),
                }
                self.data["emails"].append(email)
                self.save()
                return Result(201, {"message_id": email["message_id"]}, committed=True)

            return apply

        return self.run_write("notifications", prepare)

    # --- venue ---

    def cancel_booking(self, booking_id: str) -> Result:
        def prepare() -> Result | Callable[[], Result]:
            if booking_id != self.data["venue"]["booking_id"]:
                return Result(404, {"error": "booking_not_found"})

            def apply() -> Result:
                venue = self.data["venue"]
                if venue["status"] == "cancelled":
                    return Result(409, {"error": "already_cancelled"})  # one-shot
                venue["status"] = "cancelled"
                self.save()
                return Result(
                    200,
                    {
                        "booking_id": booking_id,
                        "status": "cancelled",
                        "fee_minor": VENUE_FEE_MINOR,
                    },
                    committed=True,
                )

            return apply

        return self.run_write("venue", prepare)

    # --- admin ---

    def reality(self) -> dict[str, Any]:
        with self.lock:
            refunds = self.data["refunds"]
            per_attendee: dict[str, int] = {}
            for r in refunds:
                per_attendee[r["attendee_id"]] = (
                    per_attendee.get(r["attendee_id"], 0) + 1
                )
            return {
                "payments": {
                    "refund_count": len(refunds),
                    "total_minor": sum(r["amount_minor"] for r in refunds),
                    "duplicate_refunds": sum(1 for n in per_attendee.values() if n > 1),
                },
                "ticketing": {
                    "cancelled": sum(
                        1
                        for a in self.data["attendees"].values()
                        if a["ticket_status"] == "cancelled"
                    )
                },
                "notifications": {"sent": len(self.data["emails"])},
                "venue": {"status": self.data["venue"]["status"]},
                "payments_api_version": self.refund_version(),
            }

    def reset(self, event_id: str, attendees: int, seed: int) -> None:
        with self.lock:
            self.data = seed_data(event_id, attendees, seed)
            self.save()

    def add_rule(self, kind: str, rule: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.data["chaos"][kind].append(rule)
            self.save()
            return self.data["chaos"]

    def set_api_version(self, system: str, version: int) -> dict[str, Any]:
        with self.lock:
            self.data["chaos"]["api_version"][system] = version
            self.save()
            return self.data["chaos"]

    def clear_chaos(self) -> dict[str, Any]:
        with self.lock:
            self.data["chaos"] = empty_chaos()
            self.save()
            return self.data["chaos"]

    def mutate_record(self, attendee_id: str, fields: dict[str, Any]) -> Result:
        with self.lock:
            att = self.data["attendees"].get(attendee_id)
            if att is None:
                return Result(404, {"error": "attendee_not_found"})
            bad = [k for k in fields if k not in MUTABLE_FIELDS]
            if bad:
                return validation_error(
                    [{"field": k, "message": "not mutable"} for k in bad]
                )
            att.update(fields)
            att["record_version"] += 1
            self.data["list_version"] += 1
            self.save()
            return Result(200, dict(att))


# --- request bodies for admin/chaos endpoints ---


class ResetBody(BaseModel):
    event_id: str = "evt_concert_001"
    attendees: int = Field(200, ge=1, le=1000)
    seed: int = 1


class DropBody(BaseModel):
    system: System
    count: int | None = Field(1, ge=1)
    nth: int | None = Field(None, ge=1)


class FailBody(BaseModel):
    system: System
    count: int | None = Field(1, ge=1)


class LatencyBody(BaseModel):
    system: System
    ms: int = Field(ge=0, le=60000)
    count: int | None = Field(None, ge=1)


class ApiVersionBody(BaseModel):
    system: Literal["payments"] = "payments"
    version: Literal[1, 2]


class MutateBody(BaseModel):
    attendee_id: str
    fields: dict[str, Any]


def create_app(
    state_path: Path | None = None, drop_sleep_s: float | None = None
) -> FastAPI:
    path = state_path or Path(
        os.environ.get("MOCK_WORLD_STATE", str(DEFAULT_STATE_PATH))
    )
    sleep_s = (
        drop_sleep_s
        if drop_sleep_s is not None
        else float(os.environ.get("MOCK_DROP_SLEEP_S", "3.5"))
    )
    world = World(path, sleep_s)
    app = FastAPI(title="Phoenix mock world")
    app.state.world = world

    # Endpoints are plain `def` so FastAPI runs them in its threadpool; the
    # chaos sleeps then block one worker thread, never the event loop.

    # Ticketing
    @app.get("/ticketing/events/{event_id}/attendees")
    def list_attendees(event_id: str) -> JSONResponse:
        with world.lock:
            if event_id != world.data["event_id"]:
                return JSONResponse({"error": "event_not_found"}, status_code=404)
            return JSONResponse(
                {
                    "event_id": event_id,
                    "version": world.data["list_version"],
                    "attendees": list(world.data["attendees"].values()),
                }
            )

    @app.get("/ticketing/attendees/{attendee_id}")
    def get_attendee(attendee_id: str) -> JSONResponse:
        with world.lock:
            att = world.data["attendees"].get(attendee_id)
            if att is None:
                return JSONResponse({"error": "attendee_not_found"}, status_code=404)
            return JSONResponse(att)

    @app.post("/ticketing/tickets/{ticket_id}/cancel")
    def cancel_ticket(ticket_id: str) -> JSONResponse:
        return world.cancel_ticket(ticket_id).response()

    # Payments
    @app.post("/payments/refunds")
    def create_refund(
        body: Annotated[Any, Body()] = None,
        dry_run: bool = False,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        if dry_run:
            return world.dry_run_refund(body).response()
        return world.create_refund(body, idempotency_key).response()

    @app.get("/payments/refunds")
    def list_refunds(
        idempotency_key: str | None = None, attendee_id: str | None = None
    ) -> JSONResponse:
        return JSONResponse(world.list_refunds(idempotency_key, attendee_id))

    @app.get("/payments/refunds/{refund_id}")
    def get_refund(refund_id: str) -> JSONResponse:
        with world.lock:
            for refund in world.data["refunds"]:
                if refund["refund_id"] == refund_id:
                    return JSONResponse(world.render_refund(refund))
        return JSONResponse({"error": "refund_not_found"}, status_code=404)

    # Notifications
    @app.post("/notifications/emails")
    def send_email(body: Annotated[Any, Body()] = None) -> JSONResponse:
        return world.send_email(body).response()

    @app.get("/notifications/emails")
    def list_emails(reference: str | None = None) -> JSONResponse:
        with world.lock:
            emails = [
                e
                for e in world.data["emails"]
                if reference is None or e["reference"] == reference
            ]
            return JSONResponse({"emails": emails})

    # Venue
    @app.post("/venue/bookings/{booking_id}/cancel")
    def cancel_booking(booking_id: str) -> JSONResponse:
        return world.cancel_booking(booking_id).response()

    @app.get("/venue/bookings/{booking_id}")
    def get_booking(booking_id: str) -> JSONResponse:
        with world.lock:
            venue = world.data["venue"]
            if booking_id != venue["booking_id"]:
                return JSONResponse({"error": "booking_not_found"}, status_code=404)
            return JSONResponse({"booking_id": booking_id, "status": venue["status"]})

    # Admin and chaos
    @app.post("/admin/reset")
    def admin_reset(body: ResetBody) -> JSONResponse:
        world.reset(body.event_id, body.attendees, body.seed)
        return JSONResponse(
            {"ok": True, "attendees": body.attendees, "event_id": body.event_id}
        )

    @app.get("/admin/reality")
    def admin_reality() -> JSONResponse:
        return JSONResponse(world.reality())

    @app.get("/admin/chaos")
    def admin_chaos() -> JSONResponse:
        with world.lock:
            return JSONResponse(world.data["chaos"])

    @app.post("/admin/chaos/drop_response_after_commit")
    def chaos_drop(body: DropBody) -> JSONResponse:
        rule = {"system": body.system, "count": body.count, "nth": body.nth}
        return JSONResponse(world.add_rule("drop_response_after_commit", rule))

    @app.post("/admin/chaos/fail_before_commit")
    def chaos_fail(body: FailBody) -> JSONResponse:
        rule = {"system": body.system, "count": body.count, "nth": None}
        return JSONResponse(world.add_rule("fail_before_commit", rule))

    @app.post("/admin/chaos/latency")
    def chaos_latency(body: LatencyBody) -> JSONResponse:
        rule = {"system": body.system, "ms": body.ms, "count": body.count, "nth": None}
        return JSONResponse(world.add_rule("latency", rule))

    @app.post("/admin/chaos/api_version")
    def chaos_api_version(body: ApiVersionBody) -> JSONResponse:
        return JSONResponse(world.set_api_version(body.system, body.version))

    @app.post("/admin/chaos/mutate_record")
    def chaos_mutate(body: MutateBody) -> JSONResponse:
        return world.mutate_record(body.attendee_id, body.fields).response()

    @app.post("/admin/chaos/clear")
    def chaos_clear() -> JSONResponse:
        return JSONResponse(world.clear_chaos())

    return app


app = create_app()

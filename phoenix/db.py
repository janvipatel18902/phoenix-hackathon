"""Mongo client, collection access, indexes/schema validation, and shared helpers.

Uses a replica-set-backed deployment (Atlas sandbox) because `state.py` relies on
multi-document transactions for invariant 6 (state transition + attempt record
committed together).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from typing import TypeVar

from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient
from pymongo.client_session import ClientSession
from pymongo.database import Database

load_dotenv()

RUNS = "runs"
TASKS = "tasks"
ATTEMPTS = "attempts"
EFFECTS = "effects"
RECOVERY_MEMORIES = "recovery_memories"
EVENTS = "events"

T = TypeVar("T")


def now_iso() -> str:
    """Millisecond-precision ISO 8601 UTC string, e.g. 2026-11-14T15:04:05.123Z."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@lru_cache(maxsize=1)
def get_client() -> MongoClient:
    uri = os.environ["MONGODB_URI"]
    return MongoClient(uri)


def get_db() -> Database:
    db_name = os.environ.get("MONGODB_DB", "phoenix")
    return get_client()[db_name]


@contextmanager
def transaction(db: Database) -> Generator[ClientSession, None, None]:
    """Run a block of work in one Mongo transaction (invariant 6)."""
    with db.client.start_session() as session, session.start_transaction():
        yield session


def run_in_transaction(db: Database, fn: Callable[[ClientSession], T]) -> T:
    """Retry-safe transaction wrapper (handles Atlas transient transaction errors).

    Takes the session from `db.client` (not the global get_client()) so a test
    can pass in any Database -- real or mocked -- and this stays correct.
    """
    with db.client.start_session() as session:
        return session.with_transaction(lambda s: fn(s))


# Schema validation (CONTRACTS.md section 4: "Schema validation on tasks ... and attempts").
_TASK_STATES = [
    "PLANNED",
    "INTENT_RECORDED",
    "SENT",
    "UNKNOWN",
    "VERIFYING",
    "VERIFIED",
    "FAILED_NOT_APPLIED",
    "NEEDS_HUMAN",
]

_TASKS_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": [
            "_id",
            "run_id",
            "seq",
            "kind",
            "system",
            "ref",
            "state",
            "idempotency_key",
            "expected_effect",
            "version",
            "created_at",
            "updated_at",
        ],
        "properties": {
            "state": {"enum": _TASK_STATES},
        },
    }
}

_ATTEMPTS_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "task_id", "run_id", "attempt_no", "phase", "ts"],
        "properties": {
            "phase": {"enum": ["intent", "result"]},
        },
    }
}


def _ensure_collection_with_validator(db: Database, name: str, validator: dict) -> None:
    """Idempotent: create the collection with a validator, or apply it via collMod."""
    if name in db.list_collection_names():
        db.command("collMod", name, validator=validator, validationLevel="moderate")
    else:
        db.create_collection(name, validator=validator, validationLevel="moderate")


def ensure_schema_validation(db: Database) -> None:
    _ensure_collection_with_validator(db, TASKS, _TASKS_VALIDATOR)
    _ensure_collection_with_validator(db, ATTEMPTS, _ATTEMPTS_VALIDATOR)


def ensure_indexes(db: Database) -> None:
    """Idempotent index creation, per CONTRACTS.md section 4."""
    db[TASKS].create_index([("run_id", ASCENDING), ("seq", ASCENDING)], unique=True)
    db[TASKS].create_index(
        [("run_id", ASCENDING), ("idempotency_key", ASCENDING)], unique=True
    )
    db[TASKS].create_index([("run_id", ASCENDING), ("state", ASCENDING)])

    db[ATTEMPTS].create_index(
        [("task_id", ASCENDING), ("attempt_no", ASCENDING), ("phase", ASCENDING)],
        unique=True,
    )

    db[EVENTS].create_index([("run_id", ASCENDING), ("ts", ASCENDING)])


def init_db(db: Database | None = None) -> Database:
    """Call once at startup (worker and api both do this)."""
    database = db if db is not None else get_db()
    ensure_schema_validation(database)
    ensure_indexes(database)
    return database

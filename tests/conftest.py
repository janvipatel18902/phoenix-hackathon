from __future__ import annotations

import mongomock
import pytest

import phoenix.db as db_module
from phoenix.db import ensure_indexes

# mongomock doesn't support create_collection(validator=...) / collMod
# (NotImplementedError), so schema validation is only exercised against a
# real Mongo deployment. The invariants themselves are enforced by state.py's
# CAS updates, not by the DB-level $jsonSchema validator, so this is safe to
# skip here.

# mongomock also doesn't support sessions/transactions at all. We patch
# run_in_transaction to run the transaction body without a session so these
# tests can verify *what* gets written (the right docs, the right state),
# which is all pure Python logic -- actual multi-document atomicity
# (invariant 6) can only be verified against a real Mongo/Atlas replica set,
# which these tests don't have access to.


@pytest.fixture
def db(monkeypatch):
    client = mongomock.MongoClient()
    database = client["phoenix_test"]
    ensure_indexes(database)
    monkeypatch.setattr(db_module, "run_in_transaction", lambda _db, fn: fn(None))
    return database

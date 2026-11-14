"""Idempotency keys and request hashing (contracts/CONTRACTS.md section 1)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from phoenix.models import TaskKind


def idempotency_key(event_id: str, kind: TaskKind, ref: str) -> str:
    """Deterministic key: phx:{event_id}:{kind}:{ref}.

    Derived only from the event and the item (never the run id or a random
    value) so reconcile can recompute it after total state loss and look the
    effect up in the external system by key.
    """
    return f"phx:{event_id}:{kind.value}:{ref}"


def request_hash(request: dict[str, Any]) -> str:
    """Stable hash of an outgoing request body, for the attempt's audit trail."""
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

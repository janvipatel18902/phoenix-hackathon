"""Recovery memories: embedding, Atlas Vector Search retrieval, save/reinforce.

A retrieved memory is only a CANDIDATE spec; it is never trusted without the
canary (invariant 7). Negative memories record specs the canary rejected.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from pymongo.database import Database
from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

from phoenix.db import RECOVERY_MEMORIES, now_iso

VECTOR_INDEX_NAME = "recovery_memories_vec"
EMBED_TIMEOUT_S = 15.0
DEFAULT_LOCAL_DIM = 256


class EmbeddingError(RuntimeError):
    """The embedding endpoint failed."""


def embedding_dim() -> int:
    raw = os.environ.get("EMBEDDING_DIM", "")
    return int(raw) if raw.isdigit() else DEFAULT_LOCAL_DIM


def min_score() -> float:
    return float(os.environ.get("MEMORY_MIN_SCORE", "0.80"))


def local_embedding(text: str, dim: int) -> list[float]:
    """Deterministic hashed bag-of-words (+bigrams). Lexical, not semantic: it
    lets the whole flow run with no embedding key, and descriptions are built
    by code so identical failures hash identically."""
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    grams = tokens + [f"{a} {b}" for a, b in itertools.pairwise(tokens)]
    vec = [0.0] * dim
    for gram in grams:
        digest = hashlib.md5(gram.encode(), usedforsecurity=False).digest()
        idx = int.from_bytes(digest[:4], "big") % dim
        vec[idx] += 1.0 if digest[4] % 2 == 0 else -1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def embed(text: str) -> list[float]:
    """Atlas AI Model API (a Voyage proxy) when EMBEDDING_BASE_URL is set,
    otherwise the local hashed embedding."""
    base = os.environ.get("EMBEDDING_BASE_URL")
    if not base:
        return local_embedding(text, embedding_dim())
    try:
        resp = httpx.post(
            f"{base.rstrip('/')}/v1/embeddings",
            headers={
                "Authorization": f"Bearer {os.environ.get('EMBEDDING_API_KEY', '')}"
            },
            json={
                "input": [text],
                "model": os.environ.get("EMBEDDING_MODEL", "voyage-4-lite"),
            },
            timeout=EMBED_TIMEOUT_S,
        )
        resp.raise_for_status()
        return [float(x) for x in resp.json()["data"][0]["embedding"]]
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        raise EmbeddingError(f"embedding call failed: {type(exc).__name__}") from exc


def cosine_score(a: list[float], b: list[float]) -> float:
    """Mapped to 0..1 like Atlas's cosine vectorSearchScore: (1 + cos) / 2."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return (1 + dot / (na * nb)) / 2 if na and nb else 0.0


@dataclass
class MemoryHit:
    memory: dict[str, Any]
    score: float


@dataclass
class Retrieval:
    hits: list[MemoryHit]
    via: str  # "vector_search" | "scan"


def retrieve(
    db: Database, system: str, description: str, polarity: str, limit: int = 3
) -> Retrieval:
    """Top matches at or above MEMORY_MIN_SCORE. Uses $vectorSearch; falls back
    to a scan if the index is missing/not ready yet (reported in `via`)."""
    query = embed(description)
    threshold = min_score()
    pipeline = [
        {
            "$vectorSearch": {
                "index": VECTOR_INDEX_NAME,
                "path": "embedding",
                "queryVector": query,
                "numCandidates": 50,
                "limit": limit,
                "filter": {"system": system, "polarity": polarity},
            }
        },
        {"$addFields": {"_score": {"$meta": "vectorSearchScore"}}},
    ]
    try:
        found = list(db[RECOVERY_MEMORIES].aggregate(pipeline))
    except (OperationFailure, NotImplementedError):
        found = []
    hits = [
        MemoryHit(
            {k: v for k, v in d.items() if k not in ("embedding", "_score")},
            d["_score"],
        )
        for d in found
        if d["_score"] >= threshold
    ]
    if hits:
        return Retrieval(hits, "vector_search")
    scanned = []
    for doc in db[RECOVERY_MEMORIES].find({"system": system, "polarity": polarity}):
        score = cosine_score(query, doc["embedding"])
        if score >= threshold:
            scanned.append(
                MemoryHit({k: v for k, v in doc.items() if k != "embedding"}, score)
            )
    scanned.sort(key=lambda h: h.score, reverse=True)
    return Retrieval(scanned[:limit], "scan")


def save_memory(
    db: Database,
    *,
    run_id: str,
    system: str,
    signature: dict[str, Any],
    description: str,
    spec: dict[str, Any],
    canary_result: dict[str, Any],
    polarity: str,
) -> tuple[str, bool]:
    """Insert, or reinforce an identical memory. Returns (memory_id, created)."""
    existing = db[RECOVERY_MEMORIES].find_one(
        {
            "system": system,
            "polarity": polarity,
            "description": description,
            "adaptation_spec": spec,
        }
    )
    counter = "success_count" if polarity == "positive" else "failure_count"
    if existing is not None:
        db[RECOVERY_MEMORIES].update_one(
            {"_id": existing["_id"]},
            {
                "$inc": {counter: 1},
                "$set": {"canary_result": canary_result, "last_used_at": now_iso()},
            },
        )
        return str(existing["_id"]), False
    memory_id = f"mem_{uuid.uuid4().hex[:10]}"
    db[RECOVERY_MEMORIES].insert_one(
        {
            "_id": memory_id,
            "polarity": polarity,
            "system": system,
            "signature": signature,
            "description": description,
            "embedding": embed(description),
            "adaptation_spec": spec,
            "canary_result": canary_result,
            "success_count": 1 if polarity == "positive" else 0,
            "failure_count": 1 if polarity == "negative" else 0,
            "source_run_id": run_id,
            "created_at": now_iso(),
            "last_used_at": None,
        }
    )
    return memory_id, True


def vector_index_definition(dimensions: int) -> dict[str, Any]:
    return {
        "fields": [
            {
                "type": "vector",
                "path": "embedding",
                "numDimensions": dimensions,
                "similarity": "cosine",
            },
            {"type": "filter", "path": "system"},
            {"type": "filter", "path": "polarity"},
        ]
    }


def ensure_vector_index(db: Database) -> str:
    """Idempotent. Returns "created", "exists" or "skipped: <why>".

    Lives here, not in db.py: Atlas Search indexes only exist on Atlas, and a
    missing EMBEDDING_DIM must not stop the engine from starting.
    """
    raw_dim = os.environ.get("EMBEDDING_DIM", "")
    if not raw_dim.isdigit():
        return "skipped: EMBEDDING_DIM is not set"
    if RECOVERY_MEMORIES not in db.list_collection_names():
        db.create_collection(RECOVERY_MEMORIES)  # search index needs the collection
    collection = db[RECOVERY_MEMORIES]
    if any(
        idx["name"] == VECTOR_INDEX_NAME for idx in collection.list_search_indexes()
    ):
        return "exists"
    collection.create_search_index(
        SearchIndexModel(
            definition=vector_index_definition(int(raw_dim)),
            name=VECTOR_INDEX_NAME,
            type="vectorSearch",
        )
    )
    return "created"

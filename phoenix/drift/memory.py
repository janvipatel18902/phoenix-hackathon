"""Recovery memory storage. For now: only the Atlas Vector Search index
(CONTRACTS.md section 4). Retrieval and saving are added with the rest of drift.
"""

from __future__ import annotations

import os

from pymongo.database import Database
from pymongo.operations import SearchIndexModel

from phoenix.db import RECOVERY_MEMORIES

VECTOR_INDEX_NAME = "recovery_memories_vec"


def vector_index_definition(dimensions: int) -> dict:
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

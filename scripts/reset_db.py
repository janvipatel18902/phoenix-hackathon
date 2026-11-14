"""Clear the app's Mongo collections (keeping recovery_memories unless asked),
re-run init_db(), and ensure the vector index. Idempotent.

Run from the repo root: PYTHONPATH=. python scripts/reset_db.py [--drop-memories]
"""

from __future__ import annotations

import sys

from pymongo.database import Database

from phoenix.db import (
    ATTEMPTS,
    EFFECTS,
    EVENTS,
    RECOVERY_MEMORIES,
    RUNS,
    TASKS,
    init_db,
)
from phoenix.drift.memory import ensure_vector_index

APP_COLLECTIONS = [RUNS, TASKS, ATTEMPTS, EFFECTS, EVENTS]


def clear_collections(db: Database, drop_memories: bool) -> dict[str, int]:
    """delete_many keeps indexes and validators in place. Returns deleted counts."""
    names = APP_COLLECTIONS + ([RECOVERY_MEMORIES] if drop_memories else [])
    return {name: db[name].delete_many({}).deleted_count for name in names}


def main() -> None:
    drop_memories = "--drop-memories" in sys.argv
    db = init_db()
    print(f"database: {db.name}")  # so an operator always sees what is being cleared
    for name, deleted in clear_collections(db, drop_memories).items():
        print(f"cleared {name}: {deleted} docs")
    if not drop_memories:
        print(
            f"kept {RECOVERY_MEMORIES}: {db[RECOVERY_MEMORIES].count_documents({})} docs"
        )
    print(f"vector index: {ensure_vector_index(db)}")


if __name__ == "__main__":
    main()

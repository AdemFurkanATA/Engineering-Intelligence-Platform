"""
src/goal-service/persistence.py

Goal Store — Phase 3.1: PostgreSQL-backed persistence with in-memory fallback.

Two implementations share the same abstract interface (GoalStore):

  InMemoryGoalStore   — default; zero config; used in tests and local dev.
  PostgreSQLGoalStore — activated when DATABASE_URL env var is set.
                        Stores the full Goal (and its nested Plan/Report) as
                        a JSONB blob in a single `goals` table.

Design decisions:
  - Single JSONB column for the goal payload keeps schema migrations trivial
    while the Goal model is still evolving.  A normalised schema (goals,
    plans, steps, reports tables) is Phase 3.2 work.
  - asyncpg is used directly (no SQLAlchemy) to keep the dependency surface
    minimal and remain consistent with the rest of the platform.
  - The store is injected into the FastAPI app at startup; the rest of the
    codebase only imports the abstract base.
"""
from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Try importing asyncpg — it may not be present in minimal test envs
try:
    import asyncpg
    _ASYNCPG_AVAILABLE = True
except ImportError:
    _ASYNCPG_AVAILABLE = False
    logger.warning("asyncpg not installed — PostgreSQL persistence unavailable")


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class GoalStore(ABC):
    """Async CRUD store for Goal records."""

    @abstractmethod
    async def save(self, goal) -> None:
        """Upsert a goal (create or update)."""

    @abstractmethod
    async def get(self, goal_id: str):
        """Return a Goal by ID, or None."""

    @abstractmethod
    async def list_all(
        self,
        status:        Optional[str] = None,
        goal_type:     Optional[str] = None,
        repository_id: Optional[str] = None,
        limit:         int           = 50,
    ) -> List:
        """Return goals, newest first, with optional filters."""

    @abstractmethod
    async def delete(self, goal_id: str) -> bool:
        """Remove a goal.  Returns True if it existed."""

    @abstractmethod
    async def find_by_idempotency_key(self, key: str):
        """Return the most recent goal with this idempotency_key, or None."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources (e.g. connection pool)."""

    @property
    @abstractmethod
    def backend(self) -> str:
        """Human-readable backend name for health checks."""


# ---------------------------------------------------------------------------
# In-memory store (default / testing)
# ---------------------------------------------------------------------------

class InMemoryGoalStore(GoalStore):
    """Thread-safe in-memory store backed by a plain dict.

    Goals are lost on restart.  Suitable for local development and testing.
    """

    def __init__(self) -> None:
        self._data: Dict[str, object] = {}

    async def save(self, goal) -> None:
        self._data[goal.goal_id] = goal

    async def get(self, goal_id: str):
        return self._data.get(goal_id)

    async def list_all(
        self,
        status:        Optional[str] = None,
        goal_type:     Optional[str] = None,
        repository_id: Optional[str] = None,
        limit:         int           = 50,
    ) -> List:
        goals = list(reversed(list(self._data.values())))
        if status:
            goals = [g for g in goals if g.status.value == status]
        if goal_type:
            goals = [g for g in goals if g.goal_type.value == goal_type]
        if repository_id:
            goals = [g for g in goals if g.repository_id == repository_id]
        return goals[:limit]

    async def delete(self, goal_id: str) -> bool:
        return self._data.pop(goal_id, None) is not None

    async def find_by_idempotency_key(self, key: str):
        """Linear scan — acceptable for in-memory store."""
        for goal in reversed(list(self._data.values())):
            if getattr(goal, 'idempotency_key', None) == key:
                return goal
        return None

    async def close(self) -> None:
        pass  # nothing to release

    @property
    def backend(self) -> str:
        return "in-memory"

    # Convenience for tests / dev
    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


# ---------------------------------------------------------------------------
# PostgreSQL store
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS goals (
    goal_id    TEXT        PRIMARY KEY,
    data       JSONB       NOT NULL,
    status     TEXT        GENERATED ALWAYS AS (data->>'status') STORED,
    goal_type  TEXT        GENERATED ALWAYS AS (data->>'goal_type') STORED,
    repo_id    TEXT        GENERATED ALWAYS AS (data->>'repository_id') STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index frequently filtered columns
CREATE INDEX IF NOT EXISTS idx_goals_status    ON goals (status);
CREATE INDEX IF NOT EXISTS idx_goals_goal_type ON goals (goal_type);
CREATE INDEX IF NOT EXISTS idx_goals_repo_id   ON goals (repo_id);
CREATE INDEX IF NOT EXISTS idx_goals_created   ON goals (created_at DESC);
"""

_UPSERT_SQL = """
INSERT INTO goals (goal_id, data, updated_at)
VALUES ($1, $2, NOW())
ON CONFLICT (goal_id) DO UPDATE
    SET data       = EXCLUDED.data,
        updated_at = NOW();
"""

_SELECT_SQL      = "SELECT data FROM goals WHERE goal_id = $1;"
_DELETE_SQL      = "DELETE FROM goals WHERE goal_id = $1 RETURNING goal_id;"


class PostgreSQLGoalStore(GoalStore):
    """asyncpg-backed PostgreSQL store.

    Goals are serialised to JSONB via ``goal.model_dump()``.
    On load, the raw dict is reconstructed into a ``Goal`` object.

    Usage:
        store = await PostgreSQLGoalStore.create(dsn)
    """

    def __init__(self, pool) -> None:
        self._pool = pool

    @classmethod
    async def create(cls, dsn: str) -> "PostgreSQLGoalStore":
        """Connect, create table if needed, return a ready store."""
        if not _ASYNCPG_AVAILABLE:
            raise RuntimeError("asyncpg is required for PostgreSQLGoalStore")

        pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=10)
        async with pool.acquire() as conn:
            await conn.execute(_CREATE_TABLE_SQL)
        logger.info("PostgreSQLGoalStore connected — table ready")
        return cls(pool)

    async def save(self, goal) -> None:
        payload = json.dumps(goal.model_dump())
        async with self._pool.acquire() as conn:
            await conn.execute(_UPSERT_SQL, goal.goal_id, payload)

    async def get(self, goal_id: str):
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_SELECT_SQL, goal_id)
        if row is None:
            return None
        return _deserialise_goal(json.loads(row["data"]))

    async def list_all(
        self,
        status:        Optional[str] = None,
        goal_type:     Optional[str] = None,
        repository_id: Optional[str] = None,
        limit:         int           = 50,
    ) -> List:
        conditions = []
        params: list = []
        p = 1

        if status:
            conditions.append(f"status = ${p}")
            params.append(status); p += 1
        if goal_type:
            conditions.append(f"goal_type = ${p}")
            params.append(goal_type); p += 1
        if repository_id:
            conditions.append(f"repo_id = ${p}")
            params.append(repository_id); p += 1

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT data FROM goals {where} ORDER BY created_at DESC LIMIT ${p};"
        params.append(limit)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        return [_deserialise_goal(json.loads(r["data"])) for r in rows]

    async def delete(self, goal_id: str) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_DELETE_SQL, goal_id)
        return row is not None

    async def find_by_idempotency_key(self, key: str):
        sql = (
            "SELECT data FROM goals "
            "WHERE data->>'idempotency_key' = $1 "
            "ORDER BY created_at DESC LIMIT 1;"
        )
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(sql, key)
        if row is None:
            return None
        return _deserialise_goal(json.loads(row["data"]))

    async def close(self) -> None:
        await self._pool.close()
        logger.info("PostgreSQLGoalStore pool closed")

    @property
    def backend(self) -> str:
        return "postgresql"


# ---------------------------------------------------------------------------
# Deserialisation helper
# ---------------------------------------------------------------------------

def _deserialise_goal(data: dict):
    """Reconstruct a Goal object from a raw dict loaded from JSONB.

    Imports Goal lazily to avoid circular imports at module load time.
    """
    from models import Goal
    return Goal.model_validate(data)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

async def create_store() -> GoalStore:
    """Create the appropriate GoalStore from environment variables.

    Environment:
        DATABASE_URL — PostgreSQL DSN.  If not set, falls back to in-memory.
                       Example: postgresql://eip_user:eip_password@localhost:5432/eip_db

    Returns a ready-to-use GoalStore instance.
    """
    dsn = os.getenv("DATABASE_URL")
    if dsn and _ASYNCPG_AVAILABLE:
        try:
            store = await PostgreSQLGoalStore.create(dsn)
            logger.info("Using PostgreSQL goal store — dsn=%s", _mask_dsn(dsn))
            return store
        except Exception as exc:
            logger.warning(
                "PostgreSQL connection failed (%s) — falling back to in-memory store", exc
            )
    else:
        if dsn and not _ASYNCPG_AVAILABLE:
            logger.warning("DATABASE_URL set but asyncpg not installed — using in-memory store")
        else:
            logger.info("DATABASE_URL not set — using in-memory goal store")

    return InMemoryGoalStore()


def _mask_dsn(dsn: str) -> str:
    """Mask password in DSN for safe logging."""
    import re
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", dsn)
